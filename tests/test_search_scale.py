from __future__ import annotations

import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from capalith.intake import scan_local_source
from capalith.semantic import SemanticUnavailable
from capalith.store import Store


class SmallParameterStore(Store):
    """Exercise the same SQLite limit failure without a huge test catalog."""

    def _connect(self, *args, **kwargs):
        connection = super()._connect(*args, **kwargs)
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
        return connection


class SearchScaleTests(unittest.TestCase):
    def test_search_does_not_require_two_sql_variables_per_catalog_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "skills"
            for index in range(40):
                name = f"document-{index:02d}"
                bundle = source_root / name
                bundle.mkdir(parents=True)
                (bundle / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: Manage documents.\n---\n# Documents\n"
                )
            store = SmallParameterStore(root / "catalog.sqlite3")
            store.initialize()
            source = store.add_local_source(source_root)
            with patch(
                "capalith.semantic.embed_documents", side_effect=SemanticUnavailable()
            ):
                scan_local_source(store, source.id)
            result = store.discover("document-05", limit=4)
            self.assertEqual("document-05", result["candidates"][0]["name"])
            self.assertEqual(4, len(result["candidates"]))


if __name__ == "__main__":
    unittest.main()
