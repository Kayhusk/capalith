from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from capalith.intake import scan_local_source
from capalith.store import Store, StoreError
from tests.test_catalog import make_catalog_skill


ADAPTER = '''
import json, os, sqlite3, time
def call(body, *, timeout, **context):
    assert timeout > 0
    if "timeout_seen" in context:
        with open(context["timeout_seen"], "a") as f:
            f.write(str(timeout) + "\\n")
    assert set(body["state"]) == {"request", "recent_context"}
    assert "context" not in body and "source_id" not in json.dumps(body)
    q = body["questions"]["which"]
    values = {n: 0.01 for n in q["criteria"]}
    narrow = "__none_applies__" in values
    mode = context.get("mode", "normal")
    if mode == "flood_output":
        with open(context["pid"], "w") as f:
            f.write(str(os.getpid()))
        remaining = memoryview(b"x" * 8_000_000)
        while remaining:
            remaining = remaining[os.write(1, remaining):]
        with open(context["accounted"], "w") as f:
            f.write("unbounded output consumed")
        time.sleep(30)
    if mode == "timeout":
        with open(context["pid"], "w") as f:
            f.write(str(os.getpid()))
        time.sleep(30)
    if mode == "transport_timeout":
        time.sleep(timeout)
        time.sleep(.05)  # model the native client's timeout accounting work
        with open(context["accounted"], "w") as f:
            f.write("timeout recorded")
        raise TimeoutError
    if mode == "error":
        raise RuntimeError("private transport detail must never reach output")
    if mode == "malformed":
        return {"model": body["model"], "answers": {"which": {"type": "choice", "choice": "alien", "probabilities": {"alien": True}}}}
    if mode == "wrong_model":
        body["model"] = "unmeasured-alias"
    if not narrow:
        if "primary_description" in context:
            assert q["criteria"]["alpha"] == context["primary_description"]
            assert all("Secondary" not in v for v in q["criteria"].values())
        if "database" in context:
            c = sqlite3.connect(context["database"], timeout=0.1)
            try:
                c.execute("BEGIN IMMEDIATE")
                c.execute("UPDATE sources SET updated_at=updated_at")
                c.commit()
            finally:
                c.close()
        if "calls" in context:
            with open(context["calls"], "a") as f:
                f.write("wide\\n")
        values["zeta"] = .9
        values["beta"] = .07
        values["alpha"] = .03
    else:
        # Deliberately reverse verification's preferred skill. It only gates
        # presence; the broader comparison must retain its measured order.
        values["alpha"] = .8
        values["__none_applies__"] = .7 if mode == "abstain" else .1
    total = sum(values.values())
    values = {n: v / total for n, v in values.items()}
    return {"model": body["model"], "answers": {"which": {"type": "choice", "choice": max(values, key=values.get), "probabilities": values}}}
'''


@patch.dict(os.environ, {"CAPALITH_SEMANTIC_MODEL": "/missing-capalith-test-model"})
class RankingTests(unittest.TestCase):
    def test_excess_worker_output_is_stopped_before_it_is_all_consumed(self):
        with self.setup_search("flood_output", timeout=.5) as (root, store, _, _):
            result = store.discover("common")
            self.assertFalse((root / "accounted").exists(), "worker must be killed before an oversized output is consumed")
            self.assertEqual({"path": "free", "reason": "provider_error"}, result["ranking"])
            pid = int((root / "pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_worker_package_parent_cannot_supply_unrelated_imports(self):
        from capalith import ranking
        with self.setup_search() as (root, store, _, _):
            parent = root / "distribution"
            package = parent / "capalith"
            shutil.copytree(Path(ranking.__file__).parent, package, ignore=shutil.ignore_patterns("__pycache__"))
            marker = root / "untrusted-sibling-imported"
            (parent / "json.py").write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\nraise RuntimeError('unrelated package sibling')\n")
            # Model a package installed beside unrelated files, after the
            # parent has already imported its trusted standard library.
            with patch.object(ranking, "__file__", str(package / "ranking.py")):
                result = store.discover("common")
            self.assertFalse(marker.exists(), "locating Capalith must not grant its whole parent import precedence")
            self.assertEqual("ai", result["ranking"]["path"])

    def test_complete_but_unnormalized_provider_distributions_are_rejected(self):
        from capalith.ranking import distribution, RankingUnavailable
        for probabilities in ({"a": 0., "b": 0.}, {"a": .8, "b": .8}):
            with self.subTest(probabilities=probabilities), self.assertRaisesRegex(RankingUnavailable, "invalid_response"):
                distribution({"model": "pinned", "answers": {"which": {"type": "choice", "choice": "a", "probabilities": probabilities}}}, {"a", "b"}, "pinned")

    @contextlib.contextmanager
    def setup_search(self, mode="normal", *, timeout=5):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("alpha", "beta", "zeta"):
                make_catalog_skill(root / "source", name, description=f"Common primary {name} instructions.")
            store = Store(root / "catalog.sqlite3")
            store.initialize()
            source = store.add_local_source(root / "source")
            scan_local_source(store, source.id)
            (root / "ranking_fixture.py").write_text(ADAPTER)
            context = {"mode": mode, "pid": str(root / "pid"), "calls": str(root / "calls"), "accounted": str(root / "accounted")}
            config = {"transport": "ranking_fixture:call", "context": context, "cache_dir": str(root / "cache"), "timeout_seconds": timeout}
            pointer = root / "ranking.json"
            pointer.write_text(json.dumps(config))
            pointer.chmod(0o600)
            pythonpath = os.pathsep.join((str(root), str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", "")))
            with patch.dict(os.environ, {"CAPALITH_RANKING_CONFIG": str(pointer), "PYTHONPATH": pythonpath}):
                yield root, Store(store.path, read_only=True), source, config

    def test_caller_checkout_cannot_shadow_worker_or_transport(self):
        for module in ("capalith", "ranking_fixture"):
            with self.subTest(module=module), self.setup_search() as (root, store, _, _):
                untrusted = root / "checkout"
                untrusted.mkdir()
                marker = root / "untrusted-import-ran"
                if module == "capalith":
                    (untrusted / module).mkdir()
                    plant = untrusted / module / "__init__.py"
                else:
                    plant = untrusted / (module + ".py")
                plant.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\nraise RuntimeError('planted checkout module')\n")
                with contextlib.chdir(untrusted):
                    result = store.discover("common")
                self.assertFalse(marker.exists(), "checkout code must never run with the managed host environment")
                self.assertEqual("ai", result["ranking"]["path"])

    def test_config_and_its_directory_must_not_be_writable_by_other_users(self):
        with self.setup_search() as (root, store, _, _):
            pointer = root / "ranking.json"
            for mode in (0o620, 0o642, 0o666):
                with self.subTest(fileMode=mode):
                    pointer.chmod(mode)
                    self.assertEqual({"path": "free", "reason": "configuration_error"}, store.discover("common")["ranking"])
            pointer.chmod(0o600)
            root.chmod(0o777)
            try:
                self.assertEqual({"path": "free", "reason": "configuration_error"}, store.discover("common")["ranking"])
            finally:
                root.chmod(0o700)
            self.assertFalse((root / "calls").exists())
            self.assertFalse((root / "cache").exists())

    def test_foreign_owned_config_or_directory_is_rejected_before_import(self):
        from capalith.ranking import settings, RankingUnavailable
        with self.setup_search() as (root, _, _, _):
            original = os.fstat
            for target in (stat.S_ISREG, stat.S_ISDIR):
                def foreign_owner(descriptor):
                    info = original(descriptor)
                    if target(info.st_mode):
                        fields = list(info)
                        fields[4] = os.geteuid() + 1
                        return os.stat_result(fields)
                    return info
                with self.subTest(target=target.__name__), patch("capalith.ranking.os.fstat", side_effect=foreign_owner), self.assertRaisesRegex(RankingUnavailable, "configuration_error"):
                    settings()
            self.assertFalse((root / "calls").exists())

    def test_transport_timeout_gets_grace_to_record_usage_before_worker_kill(self):
        with self.setup_search("transport_timeout", timeout=.5) as (root, store, _, _):
            result = store.discover("common")
            self.assertEqual({"path": "free", "reason": "provider_timeout"}, result["ranking"])
            self.assertTrue((root / "accounted").is_file(), "the transport must finish timeout accounting before a forced kill")
            self.assertEqual("timeout recorded", (root / "accounted").read_text())

    def test_worker_import_startup_reduces_transport_time_budget(self):
        with self.setup_search(timeout=2) as (root, store, _, config):
            adapter = root / "ranking_fixture.py"
            adapter.write_text("import time\ntime.sleep(.3)\n" + ADAPTER)
            config["context"]["timeout_seen"] = str(root / "timeout-seen")
            (root / "ranking.json").write_text(json.dumps(config))
            result = store.discover("common")
            self.assertEqual("ai", result["ranking"]["path"])
            budgets = [float(v) for v in (root / "timeout-seen").read_text().splitlines()]
            self.assertEqual(2, len(budgets))
            self.assertTrue(0 < budgets[1] <= budgets[0] < 1.8, budgets)

    def test_expired_views_are_removed_and_continuations_do_not_spend_again(self):
        import sqlite3
        from capalith.ranking import settings
        with self.setup_search() as (root, store, _, _):
            first = store.discover("common", limit=1)
            config = settings()
            self.assertIsNotNone(config)
            future = time.time_ns() + int(config.policy.get("cache_max_age_seconds", 3600) + 1) * 1_000_000_000
            with patch("capalith.ranking.time.time_ns", return_value=future), self.assertRaisesRegex(StoreError, "stale_view"):
                store.discover("common", offset=1, view_id=first["retrieval"]["view_id"])
            with contextlib.closing(sqlite3.connect(root / "cache/views.sqlite3")) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM ranking_views").fetchone()[0])
            self.assertNotIn(b"common", (root / "cache/views.sqlite3").read_bytes(), "expired query bytes are cleared from cache pages")
            self.assertEqual("wide\n", (root / "calls").read_text())

    def test_ai_search_and_continuation_use_one_order_across_new_store_instances(self):
        with self.setup_search() as (root, store, source, config):
            before = store.path.read_bytes()
            first = store.discover("common", limit=1)
            self.assertEqual("ai", first["ranking"]["path"])
            self.assertEqual("zeta", first["candidates"][0]["name"])
            self.assertEqual("ai_relevance", first["candidates"][0]["matches"][0]["field"])
            self.assertEqual(.9, first["candidates"][0]["score"]["ai_relevance"])
            second = Store(store.path, read_only=True).discover("common", limit=1, offset=1, view_id=first["retrieval"]["view_id"])
            self.assertEqual("beta", second["candidates"][0]["name"])
            self.assertEqual(first["retrieval"]["view_id"], second["retrieval"]["view_id"])
            self.assertEqual("wide\n", (root / "calls").read_text())
            self.assertEqual(before, store.path.read_bytes(), "the catalog stays read-only; ranking views have separate custody")
            with self.assertRaisesRegex(StoreError, "stale_view"):
                store.discover("other request", offset=1, view_id=first["retrieval"]["view_id"])
            writable = Store(store.path)
            writable.set_source_status(source.id, "disabled")
            with self.assertRaisesRegex(StoreError, "stale_view"):
                store.discover("common", offset=1, view_id=first["retrieval"]["view_id"])
            self.assertEqual("wide\n", (root / "calls").read_text(), "stale continuations never spend again")

    def test_abstention_stays_empty_including_recommendation(self):
        with self.setup_search("abstain") as (_, store, _, _):
            result = store.discover("common")
            self.assertTrue(result["ranking"]["abstained"])
            self.assertEqual([], result["candidates"])
            self.assertEqual("not_found", result["recommendation"]["status"])
            self.assertEqual([], result["recommendation"]["skills"])
            self.assertFalse(result["retrieval"]["has_more"])

    def test_cli_and_mcp_dispatch_use_the_same_configured_ranking(self):
        from tests.test_cli import invoke
        from capalith.mcp_server import _store_call
        with self.setup_search() as (root, store, _, _):
            status, cli, error = invoke(store.path, "discover", "common")
            self.assertEqual((0, ""), (status, error))
            mcp = _store_call(store.path, "discover", {"query": "common", "detail": "full"})
            self.assertEqual("ai", cli["ranking"]["path"])
            self.assertEqual(cli, mcp)
            self.assertEqual("wide\n", (root / "calls").read_text())

    def test_bad_configuration_and_corrupt_view_fail_to_free_without_new_call(self):
        import sqlite3
        with self.setup_search() as (root, store, _, config):
            first = store.discover("common")
            self.assertEqual("ai", first["ranking"]["path"])
            with contextlib.closing(sqlite3.connect(root / "cache/views.sqlite3")) as connection:
                connection.execute("UPDATE ranking_views SET payload='{}'")
                connection.commit()
            actual = store.discover("common")
            self.assertEqual("cache_unavailable", actual["ranking"]["reason"])
            self.assertEqual("wide\n", (root / "calls").read_text())
            config["none_threshold"] = True
            (root / "ranking.json").write_text(json.dumps(config))
            self.assertEqual("configuration_error", store.discover("common")["ranking"]["reason"])

    def test_unavailable_and_malformed_provider_use_free_results_with_reason(self):
        for mode, reason in (("error", "provider_error"), ("malformed", "invalid_response"), ("wrong_model", "invalid_response")):
            with self.subTest(mode=mode), self.setup_search(mode) as (_, store, _, _):
                with patch.dict(os.environ, {"CAPALITH_RANKING_CONFIG": ""}):
                    free = store.discover("common")
                actual = store.discover("common")
                self.assertEqual(free["candidates"], actual["candidates"])
                self.assertEqual({"path": "free", "reason": reason}, actual["ranking"])
                self.assertNotIn("private transport detail", json.dumps(actual))

    def test_timeout_kills_and_reaps_worker_and_returns_free_ranking(self):
        with self.setup_search("timeout", timeout=.5) as (root, store, _, _):
            started = time.monotonic()
            result = store.discover("common")
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual({"path": "free", "reason": "provider_timeout"}, result["ranking"])
            pid = int((root / "pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_source_scope_and_priority_are_resolved_before_provider_and_sqlite_is_closed(self):
        with self.setup_search() as (root, store, first, config):
            make_catalog_skill(root / "secondary", "alpha", description="Secondary excluded description.")
            writable = Store(store.path)
            second = writable.add_local_source(root / "secondary")
            scan_local_source(writable, second.id)
            config["context"].update({"database": str(store.path), "primary_description": "Common primary alpha instructions."})
            (root / "ranking.json").write_text(json.dumps(config))
            result = store.discover("common", source_ids=(first.id, second.id))
            self.assertEqual("ai", result["ranking"]["path"], "provider must be able to commit on a second connection")
            self.assertTrue(all(c["source_id"] == first.id for c in result["candidates"]))

    def test_free_view_continues_without_provider_retry_and_missing_route_creates_no_cache(self):
        with self.setup_search("error") as (root, store, _, config):
            first = store.discover("common", limit=1)
            config["context"]["mode"] = "normal"
            (root / "ranking.json").write_text(json.dumps(config))
            second = store.discover("common", limit=1, offset=1, view_id=first["retrieval"]["view_id"])
            self.assertEqual("continued_free_view", second["ranking"]["reason"])
            self.assertFalse((root / "calls").exists())
            with patch.dict(os.environ, {"CAPALITH_RANKING_CONFIG": ""}):
                result = store.discover("common")
                self.assertEqual("not_configured", result["ranking"]["reason"])


if __name__ == "__main__":
    unittest.main()
