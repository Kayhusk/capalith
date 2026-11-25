from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from capalith.intake import scan_local_source
from capalith.semantic import SemanticUnavailable
from capalith.store import Store, StoreError


def write_skill(root: Path, name: str, word: str) -> None:
    bundle = root / name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {word}.\n---\n# Instructions\n{word}\n"
    )


class SearchRefreshTests(unittest.TestCase):
    def test_warm_search_observes_another_connections_wal_refresh_and_source_changes(
        self,
    ):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "capalith.semantic.embed_documents", side_effect=SemanticUnavailable()
            ),
        ):
            root = Path(directory)
            write_skill(root / "a", "first-tool", "oldword")
            write_skill(root / "b", "second-tool", "otherword")
            database = root / "catalog.sqlite3"
            store = Store(database)
            store.initialize()
            a = store.add_local_source(root / "a")
            b = store.add_local_source(root / "b")
            scan_local_source(store, a.id)
            scan_local_source(store, b.id)
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    "wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                )
            finally:
                connection.close()

            def names(query, scope=None):
                return [
                    r["name"]
                    for r in Store(database, read_only=True).discover(
                        query, source_ids=scope
                    )["candidates"]
                ]

            self.assertEqual(["first-tool"], names("oldword", (a.id,)))
            self.assertEqual(["second-tool"], names("otherword", (b.id,)))
            self.assertEqual(["first-tool"], names("oldword", (a.id,)))
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(lambda _: names("oldword", (a.id,)), range(4))
                )
            self.assertEqual([["first-tool"]] * 4, results)
            write_skill(root / "a", "first-tool", "newword")
            scan_local_source(Store(database), a.id)
            self.assertEqual([], names("oldword", (a.id,)))
            self.assertEqual(["first-tool"], names("newword", (a.id,)))
            store.set_source_status(a.id, "disabled")
            self.assertEqual([], names("newword"))
            with self.assertRaisesRegex(StoreError, "source_unavailable"):
                names("newword", (a.id,))
            store.set_source_status(a.id, "enabled")
            self.assertEqual(["first-tool"], names("newword"))

    def test_warm_search_reopens_a_replaced_catalog_instead_of_serving_old_data(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "capalith.semantic.embed_documents", side_effect=SemanticUnavailable()
            ),
        ):
            root = Path(directory)
            write_skill(root / "a", "first-tool", "oldword")
            write_skill(root / "b", "second-tool", "newword")
            database = root / "catalog.sqlite3"
            replacement = root / "replacement.sqlite3"
            for path, source_root in (
                (database, root / "a"),
                (replacement, root / "b"),
            ):
                store = Store(path)
                store.initialize()
                source = store.add_local_source(source_root)
                scan_local_source(store, source.id)
            self.assertEqual(
                "first-tool",
                Store(database).discover("oldword")["candidates"][0]["name"],
            )
            os.replace(replacement, database)
            self.assertEqual([], Store(database).discover("oldword")["candidates"])
            self.assertEqual(
                "second-tool",
                Store(database).discover("newword")["candidates"][0]["name"],
            )


if __name__ == "__main__":
    unittest.main()
