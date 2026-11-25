"""A local usage log of counts, never of text. Off by default (`usage.enabled`); no network.

One JSON line per search in `<usage folder>/usage.jsonl`: the time (UTC), the tool, the ranking path and its reason,
how many results, the status word, the duration, the setup error code (or "none") and a random id of this install.
Every value is a number, a time, or a word from a fixed list or a lowercase code; the query, the skill text, a path
and an account never reach the file. With `usage.skill_names` on (the owner's choice) the names of the first three
results are added, each cut to a safe name.

The folder follows the rules of the other private folders (the ranking cache's): an absolute folder from the
user-owned settings, opened one step at a time without following a symbolic link, made owner-only (0700), the files 0600.
Logging can never fail or slow a search: it never waits (every lock is non-blocking), every error is swallowed, and every
line that did not land is counted (`failures()`).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from capalith.settings import checked_folder, load_settings
from capalith.store import Store

LOG_NAME = "usage.jsonl"
ID_NAME = "install-id"
LOCK_NAME = "usage.lock"
TOOLS = frozenset({"discover", "cli"})
STATUSES = frozenset({"exact", "good", "possible", "weak", "close", "no_match"})
# The setup and call errors the servers name; anything else is written as "other".
ERROR_CODES = frozenset({
    "artifact_not_found", "catalog_newer", "catalog_setup_failed", "database_unavailable", "index_required",
    "internal_error", "invalid_request", "no_skill_sources", "resource_not_found", "settings_error",
    "settings_untrusted", "source_unavailable", "stale_artifact", "stale_view",
})
# Every reason the ranking can give for a free search (RankingUnavailable codes and the store's own); a drift test keeps
# this in step with the source. Anything else is written as "other", never as free text.
REASONS = frozenset({
    "cache_unavailable", "configuration_error", "continued_free_view", "empty_roster", "invalid_response",
    "missing_key", "not_configured", "payload_limit", "provider_error", "provider_timeout", "reserved_name",
})
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_TIME = re.compile(r'"t":"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)"')
_ID = re.compile(r"[0-9a-f]{16}")

_lock = threading.Lock()
_failures = 0
_install_ids: dict[str, str] = {}


def failures() -> int:
    """How many log writes failed in this process (each was swallowed)."""
    return _failures


def enabled() -> bool:
    try:
        return bool(load_settings()["usage"]["enabled"])
    except Exception:
        return False


def folder() -> Path:
    configured = load_settings()["usage"]["dir"]
    if configured:
        return checked_folder(configured, "usage.dir")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    if not data_home.is_absolute():
        raise ValueError("XDG_DATA_HOME must be an absolute path")
    return data_home / "capalith" / "usage"


def _word(value: object, allowed: frozenset[str] | None = None, fallback: str = "other") -> str:
    if type(value) is str and _CODE.fullmatch(value) and (allowed is None or value in allowed):
        return value
    return fallback


def _open_folder(directory: Path, *, create: bool) -> int:
    """The folder as a descriptor, opened one part at a time without following a symbolic link (the ranking cache's way).
    It must be the user's own and closed to group and others."""
    descriptor = Store(directory / LOG_NAME)._open_parent(create=create)
    information = os.fstat(descriptor)
    if information.st_uid != os.getuid() or information.st_mode & 0o077:
        os.close(descriptor)
        raise PermissionError("usage folder is not private to its owner")
    return descriptor


def _open(name: str, flags: int, folder_descriptor: int) -> int:
    return os.open(name, flags | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=folder_descriptor)


def _install_id(directory: Path, folder_descriptor: int) -> str:
    key = str(directory)
    known = _install_ids.get(key)
    if known:
        return known
    try:
        with os.fdopen(_open(ID_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL, folder_descriptor), "w") as handle:
            handle.write(secrets.token_hex(8) + "\n")
    except FileExistsError:
        pass
    with os.fdopen(_open(ID_NAME, os.O_RDONLY, folder_descriptor)) as handle:
        value = handle.read().strip()
    if not _ID.fullmatch(value):
        raise ValueError("install id is not valid")
    _install_ids[key] = value
    return value


def _trim(folder_descriptor: int, days: int, now: datetime) -> None:
    """Remove the lines older than the retention, only when the oldest line is already too old, and only when nobody else
    holds the lock: a busy lock skips the trim (the next write tries again)."""
    try:
        reader = _open(LOG_NAME, os.O_RDONLY, folder_descriptor)
    except FileNotFoundError:
        return
    with os.fdopen(reader, "rb") as handle:
        head = handle.read(160).decode("utf-8", "replace")
    first = _TIME.search(head)
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if first is not None and first.group(1) >= cutoff:
        return
    lock = _open(LOCK_NAME, os.O_RDWR | os.O_CREAT, folder_descriptor)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with os.fdopen(_open(LOG_NAME, os.O_RDONLY, folder_descriptor), "rb") as handle:
            kept = [line for line in handle.read().decode("utf-8", "replace").splitlines()
                    if (found := _TIME.search(line[:160])) and found.group(1) >= cutoff]
        temporary = f"{LOG_NAME}.{os.getpid()}.tmp"
        with os.fdopen(_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, folder_descriptor), "w") as handle:
            handle.write("".join(line + "\n" for line in kept))
        os.replace(temporary, LOG_NAME, src_dir_fd=folder_descriptor, dst_dir_fd=folder_descriptor)
    finally:
        os.close(lock)


def _append(folder_descriptor: int, data: bytes) -> None:
    """One short write in append mode, under a shared lock taken before the log is opened, so a trim (exclusive) can
    never replace the file between the open and the write. A trim in progress means this line is dropped, not waited for."""
    lock = _open(LOCK_NAME, os.O_RDWR | os.O_CREAT, folder_descriptor)
    try:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        descriptor = _open(LOG_NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT, folder_descriptor)
        try:
            if os.write(descriptor, data) != len(data):
                raise OSError("short write")
        finally:
            os.close(descriptor)
    finally:
        os.close(lock)


def _line(tool: str, started: float, result: dict[str, Any] | None, error: str | None,
          directory: Path, folder_descriptor: int, now: datetime, names: bool) -> str:
    ranking = (result or {}).get("ranking") or {}
    candidates = (result or {}).get("candidates") or []
    status = "other"
    if result is not None:
        try:
            from capalith.answer import judge
            status = _word(judge(result, load_settings()["answer"])[0], STATUSES)
        except Exception:
            status = "other"
    record: dict[str, Any] = {
        "t": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": _word(tool, TOOLS),
        "path": _word(ranking.get("path"), frozenset({"ai", "free"}), "none"),
        "reason": _word(ranking.get("reason"), REASONS) if ranking.get("reason") else None,
        "n": len(candidates) if isinstance(candidates, list) else 0,
        "status": "error" if error else status,
        "ms": max(0, round((time.perf_counter() - started) * 1000)),
        "error": _word(error, ERROR_CODES) if error else "none",
        "id": _install_id(directory, folder_descriptor),
    }
    if names:
        record["names"] = [c["name"] for c in candidates[:3]
                           if isinstance(c, dict) and type(c.get("name")) is str and _NAME.fullmatch(c["name"])]
    return json.dumps(record, separators=(",", ":")) + "\n"


def record_search(tool: str, started: float, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    """Add one line for a finished search (`started` is its `time.perf_counter()` start). Never raises, never waits."""
    global _failures
    try:
        settings = load_settings()["usage"]
        if not settings["enabled"]:
            return
        directory = folder()
        now = datetime.now(timezone.utc)
        descriptor = _open_folder(directory, create=True)
        try:
            line = _line(tool, started, result, error, directory, descriptor, now, settings["skill_names"])
            _trim(descriptor, settings["retention_days"], now)
            _append(descriptor, line.encode())
        finally:
            os.close(descriptor)
    except Exception:
        with _lock:
            _failures += 1


def _percentile(sorted_values: list[int], fraction: float) -> int:
    rank = max(1, -(-int(fraction * 100) * len(sorted_values) // 100))   # nearest rank, whole numbers
    return sorted_values[min(rank, len(sorted_values)) - 1]


def summarize(directory: Path | None = None) -> dict[str, Any]:
    """One entry per UTC day from the log and nothing else: searches, ai, free by reason, errors, p50 and p95 in ms."""
    directory = directory or folder()
    days: dict[str, dict[str, Any]] = {}
    durations: dict[str, list[int]] = {}
    skipped = 0
    log = directory / LOG_NAME
    try:
        descriptor = _open_folder(directory, create=False)
    except FileNotFoundError:
        descriptor = None
    try:
        reader = _open(LOG_NAME, os.O_RDONLY, descriptor) if descriptor is not None else None
    except FileNotFoundError:
        reader = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if reader is not None:
        with os.fdopen(reader, "r", encoding="utf-8", errors="replace") as handle:
            for text in handle:
                try:
                    line = json.loads(text)
                    day = line["t"][:10]
                    path, reason, ms = line["path"], line["reason"], int(line["ms"])
                    failed = line["error"] != "none"
                except (ValueError, KeyError, TypeError):
                    skipped += 1
                    continue
                entry = days.setdefault(day, {"day": day, "searches": 0, "ai": 0, "free": 0, "free_reasons": {}, "errors": 0})
                entry["searches"] += 1
                if failed:
                    entry["errors"] += 1
                if path == "ai":
                    entry["ai"] += 1
                elif path == "free":
                    entry["free"] += 1
                    key = reason or "none"
                    entry["free_reasons"][key] = entry["free_reasons"].get(key, 0) + 1
                durations.setdefault(day, []).append(ms)
    for day, entry in days.items():
        values = sorted(durations[day])
        entry["p50_ms"], entry["p95_ms"] = _percentile(values, 0.5), _percentile(values, 0.95)
    return {"days": [days[day] for day in sorted(days)], "skipped_lines": skipped, "log": str(log)}


def render(summary: dict[str, Any]) -> str:
    """The plain form of `summarize`: one line per day."""
    if not summary["days"]:
        return "no searches logged"
    lines = []
    for entry in summary["days"]:
        reasons = ", ".join(f"{name} {count}" for name, count in sorted(entry["free_reasons"].items()))
        lines.append(f"{entry['day']}  searches {entry['searches']}  ai {entry['ai']}  free {entry['free']}"
                     + (f" ({reasons})" if reasons else "")
                     + f"  errors {entry['errors']}  p50 {entry['p50_ms']} ms  p95 {entry['p95_ms']} ms")
    if summary["skipped_lines"]:
        lines.append(f"{summary['skipped_lines']} unreadable lines skipped")
    return "\n".join(lines)
