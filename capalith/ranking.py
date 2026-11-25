"""Explicit search ranking with opt-in user-key or host-managed transport.

Only configured hosts send a request. No provider I/O occurs in a catalog
transaction. The bounded child owns one search; SQLite stores immutable views
separately so CLI invocations and MCP reconnects can continue the same order.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from capalith.catalog import skill_markdown_body
from capalith._ranking_errors import RankingUnavailable
from capalith.settings import RANKING_KEYS, SettingsError, checked_folder, load_settings, load_settings_file


def load_policy() -> dict[str, Any]:
    policy = json.loads(Path(__file__).with_name("ranking-policy.json").read_text())
    supplied = load_settings()["ranking"]
    if set(supplied) - RANKING_KEYS:
        raise RankingUnavailable("configuration_error")
    for key,value in supplied.items():
        if key == "endpoint":
            try:
                if type(value) is not str or any(ord(c) <= 32 or ord(c) == 127 for c in value):
                    raise ValueError
                endpoint = urlsplit(value)
                if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username
                        or endpoint.password or endpoint.query or endpoint.fragment):
                    raise ValueError
                endpoint.port
            except ValueError:
                raise RankingUnavailable("configuration_error") from None
            policy["typesafe"][key] = value
        elif key in ("enable_env", "key_env", "transport"):
            if type(value) is not str or not value:
                raise RankingUnavailable("configuration_error")
            if key.endswith("_env") and not re.fullmatch(r"[A-Za-z_]\w*", value):
                raise RankingUnavailable("configuration_error")
            policy["typesafe"][key] = value
        elif key == "instructions":
            names = {"wide":"wide_instructions", "verify":"verify_instructions", "none_description":"none_description"}
            if type(value) is not dict or set(value) - names.keys() or any(type(v) is not str or not v for v in value.values()):
                raise RankingUnavailable("configuration_error")
            policy.update({names[k]:v for k,v in value.items()})
        elif key in ("model", "cache_dir"):
            if type(value) is not str or not value or (key == "cache_dir" and not Path(value).is_absolute()):
                raise RankingUnavailable("configuration_error")
            policy[key] = value
        else:
            if (type(value) not in (int,float) or not math.isfinite(value) or value <= 0
                    or (key in ("shortlist","body_excerpt_chars","cache_views","wide_candidates") and type(value) is not int)):
                raise RankingUnavailable("configuration_error")
            policy[key] = value
    if policy["timeout_seconds"] > policy["max_timeout_seconds"] or policy["none_threshold"] > 1:
        raise RankingUnavailable("configuration_error")
    # The API takes at most max_choices choices in one question; the narrow call also carries the none key.
    if policy["wide_candidates"] > policy["max_choices"] - 1:
        raise RankingUnavailable("configuration_error")
    return policy


def identity(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    transport: str
    context: dict[str, Any]
    policy: dict[str, Any]
    cache: Path

    @property
    def identity(self) -> str:
        return identity({"transport": self.transport, "context": self.context, "policy": self.policy})


def settings() -> Settings | None:
    pointer = os.environ.get("CAPALITH_RANKING_CONFIG")
    try:
        from capalith.store import Store, StoreError
        policy = load_policy()
        builtin = policy["typesafe"]
        if not pointer:
            mode = os.environ.get(builtin["enable_env"], "")
            if not mode:
                return None
            if mode != builtin["enable_value"]:
                raise ValueError("configuration")
            if not os.environ.get(builtin["key_env"], "").strip():
                raise RankingUnavailable("missing_key")
            config = {"transport": builtin["transport"]}
        else:
            path = Path(pointer).expanduser()
            if not path.is_absolute():
                raise ValueError("configuration")
            config = load_settings_file(path, format="json")
        if type(config) is not dict or set(config) - {"transport", "context", "cache_dir", "timeout_seconds", "none_threshold"}:
            raise ValueError("configuration")
        transport = config["transport"]
        if type(transport) is not str or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", transport):
            raise ValueError("transport")
        context = config.get("context", {})
        if type(context) is not dict or {"body", "timeout"} & context.keys():
            raise ValueError("context")
        if transport == builtin["transport"] and context:
            raise ValueError("builtin transport has no context options")
        for key in ("timeout_seconds", "none_threshold"):
            if key in config:
                policy[key] = config[key]
        for key, low, high in (("timeout_seconds", 0, policy["max_timeout_seconds"]), ("none_threshold", 0, 1)):
            value = policy[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not low < value <= high:
                raise ValueError(key)
        cache_location = config.get("cache_dir") or policy.get("cache_dir")
        if cache_location is None:
            cache_location = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "capalith" / "ranking"
        cache = checked_folder(str(cache_location), "ranking.cache_dir")
        return Settings(transport, context, policy, cache / "views.sqlite3")
    except (OSError, ValueError, KeyError, TypeError, StoreError, SettingsError):
        raise RankingUnavailable("configuration_error") from None


def questions(skills: dict[str, dict[str, str]], policy: dict[str, Any], shortlist: list[str] | None = None) -> dict[str, Any]:
    if shortlist is None:
        criteria = {n: s["description"] for n, s in skills.items()}
        instructions = policy["wide_instructions"]
    else:
        criteria = {n: skills[n]["description"] + "\n" + skill_markdown_body(skills[n]["skill_text"])[:policy["body_excerpt_chars"]] for n in shortlist}
        criteria[policy["none_key"]] = policy["none_description"]
        instructions = policy["verify_instructions"]
    return {"which": {"type": "choice", "instructions": instructions, "criteria": criteria}}


def distribution(answer: object, names: set[str], model: str) -> dict[str, float]:
    if not isinstance(answer, dict) or answer.get("model") != model:
        raise RankingUnavailable("invalid_response")
    try:
        choice = answer["answers"]["which"]
        values = choice["probabilities"]
        if choice["type"] != "choice" or choice["choice"] not in names or set(values) != names:
            raise ValueError("choice")
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values.values()):
            raise ValueError("probability")
        if not math.isclose(math.fsum(values.values()), 1.0, rel_tol=1e-6, abs_tol=1e-6):
            raise ValueError("distribution")
        return {n: float(v) for n, v in values.items()}
    except (KeyError, TypeError, ValueError):
        raise RankingUnavailable("invalid_response") from None


# The longest text json.dumps writes for a float (24 characters). The worker payload carries the real monotonic
# deadline, which is never longer, so a payload that fits with this value fits when it is sent.
_DEADLINE_BOUND = -1.2345678901234567e-308


def request_body(query: str, skills: dict[str, dict[str, str]], policy: dict[str, Any], names: list[str] | None = None) -> dict[str, Any]:
    """The one request the provider receives: the wide one, or (with `names`) the verification one."""
    return {"model": policy["model"], "state": {"request": query, "recent_context": ""}, "questions": questions(skills, policy, names)}


def worker_payload(query: str, skills: dict[str, dict[str, str]], config: Settings, deadline: float) -> dict[str, Any]:
    return {"query": query, "skills": skills, "transport": config.transport, "context": config.context, "policy": config.policy, "deadline": deadline}


def choose_roster(query: str, skills: dict[str, dict[str, str]], free_order: list[str], config: Settings) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """The skills the first (wide) call sends, and a report of it.

    When every searched skill fits (at most `wide_candidates`, and both requests and the worker payload
    under their byte caps) the request is exactly the unbounded one. Otherwise the roster is the free
    search's own order for this query, cut to `wide_candidates` and then, from the bottom, until it fits.
    Both requests are checked before the first provider call: the wide one, and the verification one in its
    worst case (the `shortlist` largest skills of the roster, as description plus body excerpt). The payload
    is checked with the longest possible deadline text. Never an error: `reason` says what trimmed it.
    """
    policy = config.policy
    total = len(skills)
    weights: dict[str, int] = {}

    def weight(name: str) -> int:
        # What this skill adds to a verification request; the sizes add up, so the largest few are the worst case.
        if name not in weights:
            text = skills[name]["description"] + "\n" + skill_markdown_body(skills[name]["skill_text"])[:policy["body_excerpt_chars"]]
            weights[name] = len(json.dumps({name: text}).encode())
        return weights[name]

    def fits(subset: dict[str, dict[str, str]]) -> bool:
        worst = sorted(subset, key=lambda name: (-weight(name), name))[:policy["shortlist"]]
        return (len(json.dumps(request_body(query, subset, policy)).encode()) <= policy["max_request_bytes"]
                and len(json.dumps(request_body(query, subset, policy, worst)).encode()) <= policy["max_request_bytes"]
                and len(json.dumps(worker_payload(query, subset, config, _DEADLINE_BOUND)).encode()) <= policy["max_cache_bytes"])

    if total <= policy["wide_candidates"] and fits(skills):
        return skills, {"total": total, "sent": total, "reason": None}
    reasons: list[str] = []
    listed = [name for name in free_order if name in skills]
    if total > policy["wide_candidates"]:
        reasons.append("wide_candidates")
        # Past the user's own breadth the free search is the only evidence of relevance: its candidates, no filler.
        ordered = listed[: policy["wide_candidates"]]
    else:
        # The whole catalog is within the breadth; only the byte caps cut it. The free search's candidates come
        # first, then the rest in name order, so as much of today's request as fits is kept.
        in_order = set(listed)
        ordered = listed + sorted(name for name in skills if name not in in_order)

    def subset(count: int) -> dict[str, dict[str, str]]:
        return {name: skills[name] for name in sorted(ordered[:count])}

    count = len(ordered)
    if not fits(subset(count)):
        reasons.append("request_bytes")
        low, high = 0, count - 1          # largest count that fits; zero fits trivially
        while low < high:
            middle = (low + high + 1) // 2
            if fits(subset(middle)):
                low = middle
            else:
                high = middle - 1
        count = low
    return subset(count), {"total": total, "sent": count, "reason": ", ".join(reasons)}


def _worker(payload: dict[str, Any]) -> dict[str, Any]:
    policy, skills = payload["policy"], payload["skills"]
    # The parent's monotonic deadline includes interpreter/import startup.
    deadline = payload["deadline"]
    module, attribute = payload["transport"].split(":")
    transport = getattr(importlib.import_module(module), attribute)
    scores: dict[str, float] = {}
    wide_scores: dict[str, float] = {}
    for shortlist in (None, True):
        names = sorted(scores, key=lambda n: (-scores[n], n))[:policy["shortlist"]] if shortlist else None
        body = request_body(payload["query"], skills, policy, names)
        request_questions = body["questions"]
        if len(json.dumps(body).encode()) > policy["max_request_bytes"]:
            raise RankingUnavailable("payload_limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        answer = transport(body, timeout=remaining, **payload["context"])
        scores = distribution(answer, set(request_questions["which"]["criteria"]), policy["model"])
        if shortlist is None:
            wide_scores = scores
    none = scores.pop(policy["none_key"])
    # Verification decides whether any offered skill fits. It does not replace
    # the broader comparison's order: that regression lost recall in evaluation.
    order = sorted(wide_scores, key=lambda n: (-wide_scores[n], n))[:policy["shortlist"]]
    return {"order": order if none < policy["none_threshold"] else [],
            "scores": {n: wide_scores[n] for n in order}, "none_probability": none}


def _exchange(command: list[str], payload: bytes, environment: dict[str, str], deadline: float, limit: int) -> subprocess.CompletedProcess:
    """Bound output while sending input; neither pipe can block the deadline."""
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, cwd="/", env=environment, bufsize=0) as child:
        try:
            output = bytearray()
            remaining_input = memoryview(payload)
            with selectors.DefaultSelector() as ready:
                os.set_blocking(child.stdin.fileno(), False)
                os.set_blocking(child.stdout.fileno(), False)
                ready.register(child.stdin, selectors.EVENT_WRITE)
                ready.register(child.stdout, selectors.EVENT_READ)
                while ready.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, 0)
                    for key, _ in ready.select(remaining):
                        if key.fileobj is child.stdin:
                            try:
                                count = os.write(key.fd, remaining_input[:65536])
                                remaining_input = remaining_input[count:]
                            except BrokenPipeError:
                                remaining_input = remaining_input[:0]
                            except BlockingIOError:
                                continue
                            if not remaining_input:
                                ready.unregister(child.stdin)
                                child.stdin.close()
                        else:
                            try:
                                chunk = os.read(key.fd, min(65536, limit - len(output) + 1))
                            except BlockingIOError:
                                continue
                            if not chunk:
                                ready.unregister(child.stdout)
                                child.stdout.close()
                                continue
                            output.extend(chunk)
                            if len(output) > limit:
                                raise RankingUnavailable("provider_error")
            code = child.wait(timeout=max(0, deadline - time.monotonic()))
            return subprocess.CompletedProcess(command, code, bytes(output))
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


def rank(query: str, skills: dict[str, dict[str, str]], config: Settings) -> dict[str, Any]:
    if not skills:
        return {"order": [], "scores": {}, "none_probability": 1.0}
    if config.policy["none_key"] in skills:
        raise RankingUnavailable("reserved_name")
    deadline = time.monotonic() + config.policy["timeout_seconds"]
    payload = worker_payload(query, skills, config, deadline)
    encoded = json.dumps(payload).encode()
    if len(encoded) > config.policy["max_cache_bytes"]:
        raise RankingUnavailable("payload_limit")
    environment = dict(os.environ)
    # Only explicitly configured absolute host roots belong on PYTHONPATH.
    # The absolute bootstrap binds our package without trusting its siblings.
    roots = [p for p in environment.get("PYTHONPATH", "").split(os.pathsep) if p and Path(p).is_absolute()]
    environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(roots))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RankingUnavailable("provider_timeout")
    try:
        bootstrap = Path(__file__).resolve().with_name("_ranking_worker.py")
        process = _exchange([sys.executable, "-P", str(bootstrap)], encoded, environment,
                            deadline + config.policy["worker_grace_seconds"], config.policy["max_worker_output_bytes"])
        if process.returncode:
            raise RankingUnavailable("provider_error")
        result = json.loads(process.stdout)
        if "error" in result:
            raise RankingUnavailable(result["error"])
        return result
    except subprocess.TimeoutExpired:
        # The exchange kills and reaps its child before raising. No background
        # request or automatic retry outlives the failed search.
        raise RankingUnavailable("provider_timeout") from None
    except (OSError, ValueError, TypeError):
        raise RankingUnavailable("provider_error") from None


class Views:
    def __init__(self, config: Settings):
        self.config = config

    def _connect(self):
        # Reuse the catalog's no-follow, private-file connection primitive.
        # This is a separate cache, never a schema change to the user's catalog.
        from capalith.store import Store
        store = Store(self.config.cache)
        parent = store._open_parent(create=True)
        os.close(parent)
        connection = store._connect()
        try:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"ranking_views"}:
                raise ValueError("not a ranking cache")
            connection.execute("CREATE TABLE IF NOT EXISTS ranking_views (input TEXT PRIMARY KEY, view TEXT NOT NULL, payload TEXT NOT NULL, created INTEGER NOT NULL)")
            # Logical expiry is enforced and expired payload bytes are cleared
            # on every cache access; no background cleaner or provider retry.
            connection.execute("PRAGMA secure_delete=ON")
            now = time.time_ns()
            cutoff = now - int(self.config.policy["cache_max_age_seconds"] * 1_000_000_000)
            connection.execute("DELETE FROM ranking_views WHERE created<=? OR created>?", (cutoff, now))
            return connection
        except BaseException:
            connection.close()
            raise

    def get(self, key: str) -> tuple[str, dict[str, Any]] | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT view,payload FROM ranking_views WHERE input=?", (key,)).fetchone()
            if row is None:
                return None
            if len(row[1].encode()) > self.config.policy["max_cache_bytes"]:
                raise ValueError("oversized view")
            result = json.loads(row[1])
            if row[0] != "d1:" + identity(result):
                raise ValueError("corrupt view")
            return str(row[0]), result
        finally:
            connection.close()

    def put(self, key: str, result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        text = json.dumps(result, sort_keys=True, separators=(",", ":"))
        if len(text.encode()) > self.config.policy["max_cache_bytes"]:
            raise ValueError("oversized view")
        view = "d1:" + identity(result)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # A simultaneous first search converges on the first accepted view.
            connection.execute("INSERT OR IGNORE INTO ranking_views VALUES(?,?,?,?)", (key, view, text, time.time_ns()))
            connection.execute("DELETE FROM ranking_views WHERE input IN (SELECT input FROM ranking_views ORDER BY created DESC LIMIT -1 OFFSET ?)", (self.config.policy["cache_views"],))
            connection.execute("COMMIT")
        finally:
            connection.close()
        cached = self.get(key)
        if cached is None:
            raise ValueError("evicted view")
        return cached


if __name__ == "__main__":
    try:
        # Host adapters must not contaminate the worker's single JSON response.
        import contextlib
        payload = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            output = _worker(payload)
    except RankingUnavailable as error:
        output = {"error": str(error)}
    except TimeoutError:
        output = {"error": "provider_timeout"}
    except Exception:
        output = {"error": "provider_error"}
    print(json.dumps(output))
