"""The first AI call sends every searched skill when they fit, and otherwise the free search's top N for the query.

Stub transport only: no paid call. The two-Choice design and its instructions are untouched.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from capalith import answer, ranking
from capalith.intake import scan_local_source
from capalith.settings import SettingsError, defaults, load_settings
from capalith.store import Store
from tests.test_catalog import make_catalog_skill

STUB = '''
import json, sqlite3
def call(body, *, timeout, **context):
    q = body["questions"]["which"]
    names = [n for n in q["criteria"]]
    narrow = "__none_applies__" in names
    with open(context["log"], "a") as f:
        f.write(json.dumps({"narrow": narrow, "count": len(names), "bytes": len(json.dumps(body).encode()), "body": body if context.get("keep_body") else None}) + "\\n")
    # SQLite must be free for a writer while the paid call is out
    c = sqlite3.connect(context["database"], timeout=0.2)
    try:
        c.execute("BEGIN IMMEDIATE"); c.execute("UPDATE sources SET updated_at=updated_at"); c.commit()
    finally:
        c.close()
    values = {n: 0.001 for n in names}
    first = sorted(n for n in names if n != "__none_applies__")[0]
    values[first] = 0.9
    if narrow:
        values["__none_applies__"] = 0.95 if context.get("abstain") else 0.01
    total = sum(values.values())
    values = {n: v / total for n, v in values.items()}
    return {"model": body["model"], "answers": {"which": {"type": "choice", "choice": max(values, key=values.get), "probabilities": values}}}
'''


@patch.dict(os.environ, {"CAPALITH_SEMANTIC_MODEL": "/missing-capalith-test-model"})
class RosterTests(unittest.TestCase):
    @contextlib.contextmanager
    def search(self, skills, *, extra=None, keep_body=False, abstain=False, settings=""):
        """skills: list of (name, description). Yields (root, read-only store, requests)."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, description in skills:
                make_catalog_skill(root / "source", name, description=description)
            store = Store(root / "catalog.sqlite3")
            store.initialize()
            source = store.add_local_source(root / "source")
            scan_local_source(store, source.id)
            (root / "roster_stub.py").write_text(STUB)
            log = root / "calls.jsonl"
            context = {"log": str(log), "database": str(store.path), "keep_body": keep_body, "abstain": abstain, **(extra or {})}
            pointer = root / "ranking.json"
            pointer.write_text(json.dumps({"transport": "roster_stub:call", "context": context, "cache_dir": str(root / "cache"), "timeout_seconds": 20}))
            pointer.chmod(0o600)
            config = root / "settings.toml"
            config.write_text("version = 1\n" + settings)
            config.chmod(0o600)
            pythonpath = os.pathsep.join((str(root), str(Path(__file__).resolve().parents[1])))
            environment = {"CAPALITH_RANKING_CONFIG": str(pointer), "PYTHONPATH": pythonpath, "CAPALITH_CONFIG": str(config)}

            def requests():
                return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

            with patch.dict(os.environ, environment):
                yield root, Store(store.path, read_only=True), requests

    def test_when_everything_fits_the_request_is_exactly_todays(self):
        skills = [(f"skill-{i:03d}", f"Common instructions number {i}.") for i in range(125)]
        with self.search(skills, keep_body=True) as (root, store, requests):
            result = store.discover("common instructions")
            self.assertEqual({"total": 125, "sent": 125, "reason": None}, result["ranking"]["roster"])
            wide = requests()[0]
            self.assertFalse(wide["narrow"])
            # today's request: every skill, in name order, with the policy's own instructions
            policy = ranking.load_policy()
            expected = {n: d for n, d in sorted((name, f"Common instructions number {int(name[6:])}.") for name, _ in skills)}
            criteria = wide["body"]["questions"]["which"]["criteria"]
            self.assertEqual(list(expected), list(criteria))
            self.assertEqual(policy["wide_instructions"], wide["body"]["questions"]["which"]["instructions"])
            self.assertEqual({"which"}, set(wide["body"]["questions"]))

    def test_more_skills_than_wide_candidates_send_the_free_searchs_top_n_and_say_so(self):
        # more matching words in later names, so the free order is not the name order
        skills = [(f"zz-{i:03d}", f"Alpha beta gamma item {i}." + " alpha beta" * (i % 7)) for i in range(300)] + [(f"aa-{i:03d}", f"Unrelated matter {i}.") for i in range(100)]
        with self.search(skills, keep_body=True) as (root, store, requests):
            free = store._discover_local("alpha beta", limit=50)
            top = {c["name"] for c in free["candidates"]}
            result = store.discover("alpha beta")
            self.assertEqual({"total": 400, "sent": 200, "reason": "wide_candidates"}, result["ranking"]["roster"])
            wide = requests()[0]
            sent = set(wide["body"]["questions"]["which"]["criteria"])
            self.assertEqual(200, len(sent))
            self.assertEqual(sorted(sent), list(wide["body"]["questions"]["which"]["criteria"]), "name order, as in the unbounded request")
            self.assertTrue(top <= sent, "the free search's own first page is always inside the roster")
            self.assertTrue(all(name.startswith("zz-") for name in sent), "only skills the free search found")
            self.assertLessEqual(wide["bytes"], ranking.load_policy()["max_request_bytes"])
            self.assertEqual("ai", result["ranking"]["path"])

    def test_fewer_free_candidates_than_n_send_only_those(self):
        skills = [(f"zz-{i:03d}", f"Needle haystack {i}.") for i in range(30)] + [(f"aa-{i:03d}", f"Unrelated matter {i}.") for i in range(370)]
        with self.search(skills) as (root, store, requests):
            result = store.discover("needle")
            self.assertEqual({"total": 400, "sent": 30, "reason": "wide_candidates"}, result["ranking"]["roster"])
            self.assertEqual(30, requests()[0]["count"])

    def test_nothing_found_by_the_free_search_sends_nothing_and_costs_nothing(self):
        skills = [(f"aa-{i:03d}", f"Unrelated matter {i}.") for i in range(300)]
        with self.search(skills) as (root, store, requests):
            result = store.discover("quasar")
            self.assertEqual({"path": "free", "reason": "empty_roster"}, result["ranking"])
            self.assertEqual([], requests())
            self.assertEqual([], result["candidates"])

    def test_the_byte_cap_trims_from_the_bottom_of_the_free_order_and_never_errors(self):
        skills = [(f"zz-{i:03d}", "Common words " + "padding " * 280 + str(i)) for i in range(140)]
        with self.search(skills) as (root, store, requests):
            policy = ranking.load_policy()
            result = store.discover("common words")
            roster = result["ranking"]["roster"]
            self.assertEqual("ai", result["ranking"]["path"])
            self.assertEqual("request_bytes", roster["reason"])
            self.assertLess(roster["sent"], 140)
            self.assertGreater(roster["sent"], 20)
            self.assertLessEqual(requests()[0]["bytes"], policy["max_request_bytes"])

    def test_when_only_bytes_cut_a_catalog_within_the_breadth_the_free_candidates_come_first_and_the_rest_fills_in_name_order(self):
        # 140 skills fit the breadth (200) but not the 240,000-byte request; the free search lists only 10 of them
        skills = [(f"aa-{i:03d}", "Padding " * 300 + f"filler {i}") for i in range(130)] + [(f"zz-{i:03d}", "Needle " + "padding " * 300 + str(i)) for i in range(10)]
        with self.search(skills, keep_body=True) as (root, store, requests):
            result = store.discover("needle")
            roster = result["ranking"]["roster"]
            self.assertEqual("request_bytes", roster["reason"])                      # not an empty reason with sent < total
            self.assertLess(roster["sent"], 140)
            self.assertGreater(roster["sent"], 10)
            sent = list(requests()[0]["body"]["questions"]["which"]["criteria"])
            self.assertEqual(sorted(sent), sent)
            self.assertTrue({f"zz-{i:03d}" for i in range(10)} <= set(sent), "the free search's own candidates are always in")
            fill = sorted(n for n in sent if n.startswith("aa-"))
            self.assertEqual([f"aa-{i:03d}" for i in range(len(fill))], fill, "the filler is the first names in name order")
            self.assertEqual(roster["sent"], len(sent))
            self.assertLessEqual(requests()[0]["bytes"], ranking.load_policy()["max_request_bytes"])

    def test_sqlite_is_free_for_a_writer_while_the_provider_call_is_out(self):
        skills = [(f"zz-{i:03d}", f"Alpha beta gamma item {i}.") for i in range(250)]
        with self.search(skills) as (root, store, requests):
            result = store.discover("alpha beta")   # the stub writes to the catalog during each call and would raise if a reader held it
            self.assertEqual("ai", result["ranking"]["path"])
            self.assertEqual(2, len(requests()))

    def test_page_two_of_a_trimmed_answer_is_the_same_view_with_no_second_provider_call(self):
        skills = [(f"zz-{i:03d}", f"Alpha beta gamma item {i}.") for i in range(300)]
        with self.search(skills) as (root, store, requests):
            first = store.discover("alpha beta", limit=1)
            calls = len(requests())
            second = store.discover("alpha beta", limit=1, offset=1, view_id=first["retrieval"]["view_id"])
            self.assertEqual(calls, len(requests()))
            self.assertEqual(first["ranking"]["roster"], second["ranking"]["roster"])
            self.assertEqual(first["retrieval"]["view_id"], second["retrieval"]["view_id"])

    def test_wide_candidates_is_a_setting_with_the_apis_limit_behind_it(self):
        skills = [(f"zz-{i:03d}", f"Alpha beta gamma item {i}.") for i in range(120)]
        with self.search(skills, settings="[ranking]\nwide_candidates = 50\n") as (root, store, requests):
            result = store.discover("alpha beta")
            self.assertEqual({"total": 120, "sent": 50, "reason": "wide_candidates"}, result["ranking"]["roster"])
        self.assertEqual(200, ranking.load_policy()["wide_candidates"])
        self.assertLess(ranking.load_policy()["wide_candidates"], ranking.load_policy()["max_choices"])
        for bad in ("255", "0", "2.5", '"many"'):
            with self.subTest(bad=bad), self.search(skills[:3], settings=f"[ranking]\nwide_candidates = {bad}\n") as (root, store, requests):
                self.assertEqual({"path": "free", "reason": "configuration_error"}, store.discover("alpha")["ranking"])

    def test_a_no_skill_answer_from_a_trimmed_roster_says_how_many_it_looked_at(self):
        skills = [(f"zz-{i:03d}", f"Alpha beta gamma item {i}.") for i in range(300)]
        with self.search(skills, abstain=True) as (root, store, requests):
            record = store.discover("alpha beta")
            self.assertEqual("ai", record["ranking"]["path"])
            self.assertTrue(record["ranking"]["abstained"])
            shaped = answer.shape(store, record, "short")
            self.assertEqual("no_match", shaped["status"])
            self.assertTrue(shaped["_text"].startswith("no_match: None of the 200 nearest skills fits this task."), shaped["_text"])
            self.assertNotIn("No skill in the catalog", shaped["_text"])
        skills = [(f"zz-{i:03d}", f"Alpha beta gamma item {i}.") for i in range(20)]
        with self.search(skills, abstain=True) as (root, store, requests):
            shaped = answer.shape(store, store.discover("alpha beta"), "short")
            self.assertTrue(shaped["_text"].startswith("no_match: No skill in the catalog fits this task."), shaped["_text"])

    def test_a_free_result_that_carries_a_roster_never_uses_the_nearest_only_wording(self):
        skills = [(f"zz-{i:03d}", f"Alpha beta gamma item {i}.") for i in range(300)]
        with self.search(skills) as (root, store, requests):
            record = store.discover("quasar")           # nothing found: an empty roster, the free answer
            self.assertEqual("free", record["ranking"]["path"])
            record["ranking"]["roster"] = {"total": 300, "sent": 200, "reason": "wide_candidates"}
            shaped = answer.shape(store, record, "short")
            self.assertTrue(shaped["_text"].startswith("no_match: No skill in the catalog"), shaped["_text"])


def _skill(name: str, description: str, body: str = "e" * 900) -> dict[str, str]:
    return {"description": description, "skill_text": f"---\nname: {name}\ndescription: x\n---\n" + body}


def _config(**changes) -> ranking.Settings:
    policy = ranking.load_policy()
    policy.update(changes)
    return ranking.Settings("roster_inproc:call", {}, policy, Path("/unused"))


def _payload(config: ranking.Settings, roster: dict, deadline: float, query: str = "alpha") -> dict:
    return {"query": query, "skills": roster, "transport": config.transport, "context": config.context,
            "policy": config.policy, "deadline": deadline}


def _size(value: object) -> int:
    return len(json.dumps(value).encode())


class RosterByteBoundTests(unittest.TestCase):
    """The roster is chosen so that both real requests and the real worker payload fit, before the first call."""

    def setUp(self):
        self.calls: list[dict] = []
        module = types.ModuleType("roster_inproc")

        def call(body, *, timeout, **context):
            self.calls.append(body)
            criteria = body["questions"]["which"]["criteria"]
            # the longest text is the most probable: the shortlist is the largest skills, the worst case
            weights = {n: len(t) for n, t in criteria.items() if n != "__none_applies__"}
            values = {n: float(w) for n, w in weights.items()}
            if "__none_applies__" in criteria:
                values["__none_applies__"] = 1.0
            total = sum(values.values())
            values = {n: v / total for n, v in values.items()}
            return {"model": body["model"], "answers": {"which": {"type": "choice", "choice": max(values, key=values.get), "probabilities": values}}}

        module.call = call
        patcher = patch.dict(sys.modules, {"roster_inproc": module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def long_skills(self, count: int, base: int = 23_000, step: int = 50) -> dict[str, dict[str, str]]:
        return {f"s{i:02d}": _skill(f"s{i:02d}", "d" * (base + step * i)) for i in range(count)}

    def run_worker(self, config, roster):
        return ranking._worker(_payload(config, roster, time.monotonic() + 60))

    def test_the_verification_request_fits_even_when_the_shortlist_is_the_largest_skills(self):
        # the reviewer's case: 20 long descriptions; the wide body of 10 fits 240,000 but the verification body does not
        # uniform 23,500 characters is the reviewer's own fixture; the varied one makes the shortlist the largest of the roster
        for label, skills in (("uniform", self.long_skills(20, base=23_500, step=0)), ("varied", self.long_skills(20, base=20_000, step=400))):
            with self.subTest(label):
                self.calls.clear()
                config = _config()
                roster, report = ranking.choose_roster("alpha", skills, list(skills), config)
                self.assertEqual("request_bytes", report["reason"])
                self.assertGreater(report["sent"], 3, "a real roster, not a vacuous empty one")
                self.run_worker(config, roster)                # raised payload_limit on the second call before the repair
                self.assertEqual([False, True], ["__none_applies__" in c["questions"]["which"]["criteria"] for c in self.calls])
                for body in self.calls:
                    self.assertLessEqual(_size(body), config.policy["max_request_bytes"])

    def test_the_narrow_limit_is_exact_at_the_boundary(self):
        skills = self.long_skills(8, base=20_000, step=100)
        config = _config()
        names = sorted(skills)
        narrow = _size({"model": config.policy["model"], "state": {"request": "alpha", "recent_context": ""},
                        "questions": ranking.questions(skills, config.policy, names)})
        wide = _size({"model": config.policy["model"], "state": {"request": "alpha", "recent_context": ""},
                      "questions": ranking.questions(skills, config.policy)})
        self.assertLess(wide, narrow, "the verification request is the binding one in this fixture")
        exact = _config(max_request_bytes=narrow)
        roster, report = ranking.choose_roster("alpha", skills, names, exact)
        self.assertEqual({"total": 8, "sent": 8, "reason": None}, report)
        self.run_worker(exact, roster)
        self.calls.clear()
        under = _config(max_request_bytes=narrow - 1)
        roster, report = ranking.choose_roster("alpha", skills, names, under)
        self.assertEqual({"total": 8, "sent": 7, "reason": "request_bytes"}, report)
        self.run_worker(under, roster)

    def test_the_worst_shortlist_is_the_largest_by_description_and_body_excerpt_together(self):
        # 11 skills, shortlist 10: the verification request drops the one smallest skill. Counting the 700-character
        # excerpt, the smallest is "v" (200 characters, no body), not "t" (100 characters plus an excerpt).
        skills = {f"b{i}": _skill(f"b{i}", "d" * 28_000) for i in range(8)}
        skills.update({"t": _skill("t", "d" * 100), "u": _skill("u", "d" * 300, body=""), "v": _skill("v", "d" * 200, body="")})
        config = _config()
        worst = [f"b{i}" for i in range(8)] + ["t", "u"]
        state = {"request": "alpha", "recent_context": ""}
        narrow = _size({"model": config.policy["model"], "state": state, "questions": ranking.questions(skills, config.policy, worst)})
        wide = _size({"model": config.policy["model"], "state": state, "questions": ranking.questions(skills, config.policy)})
        self.assertLess(wide, narrow)
        order = sorted(skills)
        self.assertEqual({"total": 11, "sent": 11, "reason": None}, ranking.choose_roster("alpha", skills, order, _config(max_request_bytes=narrow))[1])
        trimmed = ranking.choose_roster("alpha", skills, order, _config(max_request_bytes=narrow - 1))[1]
        self.assertEqual("request_bytes", trimmed["reason"])
        self.assertLess(trimmed["sent"], 11)

    def exchange(self, config, roster):
        """rank() up to the child: the real payload is serialized and its size checked, then a stub child answers."""
        done = subprocess.CompletedProcess([], 0, json.dumps({"order": [], "scores": {}, "none_probability": 1.0}).encode())
        with patch.object(ranking, "_exchange", return_value=done) as exchange:
            ranking.rank("alpha", roster, config)
        self.assertEqual(1, exchange.call_count)

    def fixed_cap(self, deadline: float, roster: dict) -> int:
        # the cap is inside the policy that the payload carries, so the cap and the size must agree
        cap = 9999
        for _ in range(6):
            size = _size(_payload(_config(max_cache_bytes=cap), roster, deadline))
            if size == cap:
                return cap
            cap = size
        self.fail("no fixed point")

    def test_the_worker_payload_is_checked_with_a_deadline_as_long_as_the_real_one(self):
        skills = {f"s{i}": _skill(f"s{i}", "d" * 300) for i in range(2)}
        cap = self.fixed_cap(0.0, skills)         # exactly full with the short placeholder deadline 0.0
        config = _config(max_cache_bytes=cap)
        roster, report = ranking.choose_roster("alpha", skills, list(skills), config)
        self.assertLess(report["sent"], 2)
        self.exchange(config, roster)             # raised payload_limit before dispatch with the placeholder deadline

    def test_a_payload_just_under_the_limit_with_the_longest_possible_deadline_is_not_trimmed(self):
        skills = {f"s{i}": _skill(f"s{i}", "d" * 300) for i in range(2)}
        cap = self.fixed_cap(ranking._DEADLINE_BOUND, skills)
        config = _config(max_cache_bytes=cap)
        roster, report = ranking.choose_roster("alpha", skills, list(skills), config)
        self.assertEqual({"total": 2, "sent": 2, "reason": None}, report)
        self.exchange(config, roster)             # the real, shorter deadline fits as well


if __name__ == "__main__":
    unittest.main()
