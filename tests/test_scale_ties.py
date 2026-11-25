"""Directed tie cases for the keys of the array search: the order must come from exactly the keys the per-row code used.

Each fixture is built so that dropping one key changes the order (checked with a mutant of the production code; see the exact-name test for the wording that was corrected).
The model is replaced by seeded query vectors, as in test_scale_equivalence.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capalith import semantic, store as store_module
from capalith.catalog import CATALOG_FORMAT_VERSION
from capalith.intake import scan_local_source
from capalith.store import Store
from tests.test_catalog import make_catalog_skill
from tests.test_scale_equivalence import CENTRES, _query_vector, _unit


def centre_of(query: str):
    return CENTRES[int(hashlib.sha256(query.encode()).hexdigest(), 16) % len(CENTRES)]


def add_vectors(store: Store, vectors: dict) -> None:
    """A vector for every ready skill (so the meaning index is complete): the given one, or a seeded far one."""
    connection = store._connect()
    try:
        rows = connection.execute("SELECT artifact_id,content_digest,name,description,skill_text FROM catalog_entries WHERE status='ready'").fetchall()
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            vector = vectors.get(row["name"], _unit(int(hashlib.sha256(row["name"].encode()).hexdigest(), 16) % 10**6))
            projection = semantic.projection(row["name"], row["description"], row["skill_text"])
            connection.execute(
                "INSERT OR REPLACE INTO catalog_semantic_vectors VALUES(?,?,?,?,?,?,?)",
                (row["artifact_id"], row["content_digest"], CATALOG_FORMAT_VERSION, semantic.MODEL_KEY,
                 semantic.projection_digest(projection), semantic.DIMENSIONS, vector.tobytes()),
            )
        connection.execute("COMMIT")
    finally:
        connection.close()


@patch.dict(os.environ, {"CAPALITH_SEMANTIC_MODEL": "/missing-capalith-test-model"})
class ScaleTieTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(store_module, "_semantic_query_vector", _query_vector)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_exact_name_leads_the_fused_order_and_decides_it_when_every_fused_score_ties(self):
        # With the default weights an exact-name skill is word rank 1 and meaning rank 1 (its flag puts it first in both lists and
        # lets it through the gate), so its fused score is the largest and the flag in the fused sort is redundant. It is NOT
        # redundant for every legal setting: with a tiny positive weight every term underflows to 0.0, all fused scores tie, and the
        # flag, then the source priority and then the name decide. (My first version of this test, and the commit message of 5da5440,
        # said the flag could never change the order: that was wrong.)
        def run(weights: str):
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                store = Store(base / "catalog.sqlite3")
                store.initialize()
                root = base / "source"
                root.mkdir()
                make_catalog_skill(root, "zzz-exact-tool", description="exact tool", body="exact tool")
                make_catalog_skill(root, "aaa-helper", description="exact tool helper", body="exact tool exact tool exact tool")
                source = store.add_local_source(root)
                scan_local_source(store, source.id)
                query = "zzz-exact-tool"
                centre = centre_of(query).astype("float64")
                add_vectors(store, {"zzz-exact-tool": _unit(9, centre, 0.6), "aaa-helper": _unit(5, centre, 0.02)})
                config = base / "settings.toml"
                config.write_text("version = 1\n" + weights)
                config.chmod(0o600)
                with patch.dict(os.environ, {"CAPALITH_CONFIG": str(config)}):
                    return Store(base / "catalog.sqlite3", read_only=True).discover(query)

        result = run("")
        self.assertEqual("sqlite_fts5_bm25_local_semantic_rrf", result["retrieval"]["method"])
        self.assertEqual(["zzz-exact-tool", "aaa-helper"], [c["name"] for c in result["candidates"]])
        top = result["candidates"][0]["score"]
        self.assertEqual((1, 1), (top["lexical_rank"], top["semantic_rank"]))
        self.assertLess(top["semantic_similarity"], 0.66, "it passed the gate only because its name is exact")
        tiny = run("[search.fusion_weights]\nlexical = 5e-324\nsemantic = 5e-324\n")
        self.assertEqual({0.0}, {c["score"]["fused_score"] for c in tiny["candidates"]}, "every fused score underflowed to a tie")
        self.assertEqual(["zzz-exact-tool", "aaa-helper"], [c["name"] for c in tiny["candidates"]], "the exact-name flag decides a tie")

    def test_source_priority_orders_equal_meaning_scores_before_the_name_does(self):
        # Two skills in two sources, identical vectors (equal similarity), no word in common with the query: only the meaning
        # list orders them. Priority is the position in the call's source scope, and it comes before the name.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            store = Store(base / "catalog.sqlite3")
            store.initialize()
            first_root, second_root = base / "one", base / "two"
            first_root.mkdir()
            second_root.mkdir()
            make_catalog_skill(first_root, "zzz-first-source", description="unrelated words", body="unrelated")
            make_catalog_skill(second_root, "aaa-second-source", description="other words", body="other")
            first = store.add_local_source(first_root)
            scan_local_source(store, first.id)
            second = store.add_local_source(second_root)
            scan_local_source(store, second.id)
            query = "quokka"
            same = _unit(11, centre_of(query).astype("float64"), 0.05)
            add_vectors(store, {"zzz-first-source": same, "aaa-second-source": same})
            reader = Store(base / "catalog.sqlite3", read_only=True)
            for scope, expected in (((first.id, second.id), ["zzz-first-source", "aaa-second-source"]),
                                    ((second.id, first.id), ["aaa-second-source", "zzz-first-source"])):
                with self.subTest(scope="first source first" if scope[0] == first.id else "second source first"):
                    result = reader.discover(query, source_ids=scope)
                    self.assertEqual("sqlite_fts5_bm25_local_semantic_rrf", result["retrieval"]["method"])
                    self.assertEqual(expected, [c["name"] for c in result["candidates"]])

    def test_the_best_row_of_a_skill_with_equal_ranks_is_the_one_with_the_lowest_path(self):
        # Two indexed files of one skill with the same text rank the same; the old statement's order (rank, artifact id, path key)
        # put the lower path first, and that row is the one the answer reports as the match.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            store = Store(base / "catalog.sqlite3")
            store.initialize()
            root = base / "source"
            root.mkdir()
            bundle = make_catalog_skill(root, "plain-skill", description="plain", body="")
            (bundle / "references").mkdir()
            for name in ("b.md", "a.md", "c.md"):
                (bundle / "references" / name).write_text("nebula evidence")
            source = store.add_local_source(root)
            scan_local_source(store, source.id)
            result = Store(base / "catalog.sqlite3", read_only=True).discover("nebula")
            self.assertEqual("sqlite_fts5_bm25", result["retrieval"]["method"])
            self.assertEqual([["references/a.md"]], [[m["path"] for m in c["matches"]] for c in result["candidates"]])

    def test_the_roster_tail_below_the_gate_is_named_by_skill_even_when_a_name_has_two_bundles(self):
        # No word matches and every meaning score is below the gate: the free order is only the best meaning scores, best first.
        # "aaa-dup" has two bundles, so the entries list is one longer than the meaning list from there on: a position in the
        # meaning list is not a position in the entries (a mix-up names the wrong skill).
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            store = Store(base / "catalog.sqlite3")
            store.initialize()
            root = base / "source"
            root.mkdir()
            make_catalog_skill(root, "aaa-dup", bundle_path="one", description="first copy", body="first copy")
            make_catalog_skill(root, "aaa-dup", bundle_path="two", description="second copy", body="second copy")
            for name in ("bbb-first", "ccc-second", "ddd-third"):
                make_catalog_skill(root, name, description=f"{name} text", body=f"{name} text")
            source = store.add_local_source(root)
            scan_local_source(store, source.id)
            query = "quokka"
            centre = centre_of(query).astype("float64")
            add_vectors(store, {
                "bbb-first": _unit(21, centre, 0.08), "ccc-second": _unit(22, centre, 0.12),
                "ddd-third": _unit(23, centre, 0.2), "aaa-dup": _unit(24, centre, 1.0),
            })
            snapshot: dict = {}
            result = Store(base / "catalog.sqlite3", read_only=True)._discover_local(query, _snapshot=snapshot, _roster=4)
            self.assertEqual([], result["candidates"], "nothing passes the gate and no word matches")
            self.assertEqual(["bbb-first", "ccc-second", "ddd-third", "aaa-dup"], snapshot["free_order"])

    def test_a_very_large_rrf_k_divides_like_python_does(self):
        # numpy integer addition would wrap near 2**63 (and round twice above 2**53); the denominators are Python's own sums.
        for rrf_k in (60, 2**53 - 3, 2**53, 2**62, 2**63 - 2, 2**63, 2**70):
            with self.subTest(rrf_k=rrf_k):
                expected = [1.0 / (rrf_k + rank) for rank in range(1, 6)]
                self.assertEqual(expected, [1.0 / float(x) for x in store_module._rank_denominators(rrf_k, 5)])
                self.assertEqual(expected, list(1.0 / store_module._rank_denominators(rrf_k, 5)))

    def test_discover_orders_by_rank_with_a_huge_rrf_k(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            store = Store(base / "catalog.sqlite3")
            store.initialize()
            root = base / "source"
            root.mkdir()
            make_catalog_skill(root, "alpha-tool", description="zeta tool helper", body="zeta tool zeta tool zeta tool")
            make_catalog_skill(root, "beta-tool", description="zeta tool helper", body="zeta")
            source = store.add_local_source(root)
            scan_local_source(store, source.id)
            centre = centre_of("zeta tool").astype("float64")
            add_vectors(store, {"alpha-tool": _unit(3, centre, 0.02), "beta-tool": _unit(4, centre, 0.05)})
            config = base / "settings.toml"
            config.write_text("version = 1\n[search]\nrrf_k = 9223372036854775800\n")
            config.chmod(0o600)
            with patch.dict(os.environ, {"CAPALITH_CONFIG": str(config)}):
                result = Store(base / "catalog.sqlite3", read_only=True).discover("zeta tool")
            self.assertEqual("sqlite_fts5_bm25_local_semantic_rrf", result["retrieval"]["method"])
            self.assertEqual(9223372036854775800, result["retrieval"]["rank_constant"])
            self.assertEqual(["alpha-tool", "beta-tool"], [c["name"] for c in result["candidates"]])


if __name__ == "__main__":
    unittest.main()
