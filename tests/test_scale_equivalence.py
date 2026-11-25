"""The search's order is the same as before the word and meaning passes were done in arrays.

`golden_order.json` holds, for seeded random catalogs (tie-heavy text, repeated names, several sources, seeded vectors
that are complete, partial or absent) and seeded random queries (some an exact skill name, some with a source scope,
every page, several page sizes), what the previous implementation (per-row sorting and fusing in Python) returned: the
names in order on every page, the retrieval method and the fused scores. It was produced by that implementation (tag
`golden_source`); this test builds the same catalogs and compares every page. The model is replaced by seeded query
vectors, so only the ranking code is compared.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capalith import semantic, store as store_module
from capalith.catalog import CATALOG_FORMAT_VERSION
from capalith.intake import scan_local_source
from capalith.store import Store
from tests.test_catalog import make_catalog_skill

VOCABULARY = ["alpha", "beta", "gamma", "delta", "kappa", "omega", "review", "deploy", "queue", "sqlite"]
GOLDEN = Path(__file__).with_name("golden_order.json")
CATALOGS, QUERIES = 12, 25


def _unit(seed: int, centre=None, noise: float = 0.0):
    import numpy

    vector = numpy.random.default_rng(seed).normal(size=semantic.DIMENSIONS)
    if centre is not None:
        vector = centre + noise * vector
    return (vector / numpy.linalg.norm(vector)).astype("<f4")


CENTRES = [_unit(1000 + k) for k in range(6)]


def build_catalog(base: Path, number: int) -> list[str]:
    """One seeded catalog under `base`; returns its source ids."""
    rng = random.Random(7000 + number)
    store = Store(base / "catalog.sqlite3")
    store.initialize()
    sources = []
    for index in range(rng.choice([1, 1, 2, 3])):
        root = base / f"src{index}"
        root.mkdir()
        for position in range(rng.randint(3, 40)):
            name = f"skill-{rng.choice(VOCABULARY)}-{rng.randint(0, 6)}"
            words = " ".join(rng.choice(VOCABULARY) for _ in range(rng.randint(1, 4)))
            try:
                bundle = make_catalog_skill(root, name, bundle_path=f"group{position % 3}/{name}-{position}", description=f"{words} helper",
                                            body=" ".join(rng.choice(VOCABULARY) for _ in range(rng.randint(0, 6))))
            except FileExistsError:
                continue
            if rng.random() < 0.4:      # more text files: several indexed rows for one skill, with equal or different ranks
                (bundle / "references").mkdir()
                for number_of_file in range(rng.randint(1, 3)):
                    (bundle / "references" / f"note{number_of_file}.md").write_text(" ".join(rng.choice(VOCABULARY) for _ in range(rng.randint(1, 5))))
        source = store.add_local_source(root)
        scan_local_source(store, source.id)
        sources.append(source.id)
    vectors = rng.choice(["all", "all", "all", "some", "none"])
    connection = store._connect()
    try:
        rows = connection.execute("SELECT artifact_id,content_digest,name,description,skill_text FROM catalog_entries WHERE status='ready'").fetchall()
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            if vectors == "none" or (vectors == "some" and rng.random() < 0.3):
                continue
            centre = CENTRES[rng.randrange(len(CENTRES))].astype("float64")
            if rng.random() < 0.3:       # identical vectors: equal similarities, so source priority, name and id break the ties
                vector = _unit(2000 + rng.randrange(3), centre, 0.05)
            else:
                vector = _unit(rng.randrange(10**6), centre, rng.choice([0.02, 0.05, 0.3]))
            projection = semantic.projection(row["name"], row["description"], row["skill_text"])
            connection.execute(
                "INSERT OR REPLACE INTO catalog_semantic_vectors VALUES(?,?,?,?,?,?,?)",
                (row["artifact_id"], row["content_digest"], CATALOG_FORMAT_VERSION, semantic.MODEL_KEY,
                 semantic.projection_digest(projection), semantic.DIMENSIONS, vector.tobytes()),
            )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return sources


def _query_vector(query: str) -> bytes:
    return CENTRES[int(hashlib.sha256(query.encode()).hexdigest(), 16) % len(CENTRES)].tobytes()


def summarize(base: Path, sources: list[str], number: int, queries: int = QUERIES) -> dict[str, list]:
    """Every page of seeded random queries, reduced to what the order is made of."""
    store = Store(base / "catalog.sqlite3", read_only=True)
    rng = random.Random(number + 99)
    connection = store._connect()
    try:
        names = [row[0] for row in connection.execute("SELECT name FROM catalog_entries")]
    finally:
        connection.close()
    summary: dict[str, list] = {}
    for index in range(queries):
        query = rng.choice(names) if rng.random() < 0.15 and names else " ".join(rng.choice(VOCABULARY) for _ in range(rng.randint(1, 5)))
        limit, scope = rng.choice([1, 2, 3, 5, 7, 10]), None
        if len(sources) > 1 and rng.random() < 0.4:
            scope = tuple(rng.sample(sources, rng.randint(1, len(sources))))
        pages, view = [], None
        for page in range(12):
            arguments = {"limit": limit, "offset": page * limit}
            if view:
                arguments["view_id"] = view
            if scope:
                arguments["source_ids"] = scope
            result = store.discover(query, **arguments)
            view = (result.get("retrieval") or {}).get("view_id") or view
            pages.append({
                "names": [candidate["name"] for candidate in result["candidates"]],
                "scores": [{key: candidate["score"].get(key) for key in ("fused_score", "lexical_rank", "semantic_rank", "semantic_similarity")}
                           for candidate in result["candidates"]],
                "method": (result.get("retrieval") or {}).get("method"),
                "semantic": (result.get("semantic") or {}).get("status"),
                "overfetch": (result.get("retrieval") or {}).get("overfetch"),
            })
            if not result["candidates"]:
                break
        summary[f"c{number}/{index}"] = pages
    return summary


def golden_run() -> dict[str, list]:
    with tempfile.TemporaryDirectory() as directory, patch.object(store_module, "_semantic_query_vector", _query_vector), \
            patch.dict(os.environ, {"CAPALITH_SEMANTIC_MODEL": "/missing-capalith-test-model"}):
        out: dict[str, list] = {}
        for number in range(CATALOGS):
            base = Path(directory) / f"c{number}"
            base.mkdir()
            out.update(summarize(base, build_catalog(base, number), number))
        return out


class ScaleEquivalenceTests(unittest.TestCase):
    def test_every_page_of_seeded_random_queries_matches_the_previous_implementation(self):
        golden = json.loads(GOLDEN.read_text())
        self.assertEqual("the previous implementation (per-row Python sort and fusion)", golden["golden_source"])
        expected = golden["pages"]
        actual = golden_run()
        self.assertEqual(set(expected), set(actual))
        methods = {page["method"] for pages in expected.values() for page in pages}
        self.assertEqual({"sqlite_fts5_bm25", "sqlite_fts5_bm25_local_semantic_rrf"}, methods, "both the word-only and the fused path are covered")
        self.assertGreater(sum(len(pages) for pages in expected.values()), 500)
        for key in sorted(expected):
            self.assertEqual(expected[key], actual[key], key)

    def test_the_cache_counts_the_integer_keys_it_keeps(self):
        rows = [{"name": f"n{i}", "artifact_id": f"a{i}", "content_digest": f"d{i}", "source_priority": 0} for i in range(1000)]
        keys = store_module._OrderKeys(rows)
        counted = store_module._prepared_size(keys, set())
        self.assertGreaterEqual(counted, keys.nbytes + 1000 * 2 * 50, "arrays, the two string lists and the id dictionary are counted, not one small object")


if __name__ == "__main__":
    unittest.main()
