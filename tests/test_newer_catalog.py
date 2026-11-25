"""A catalog written by a newer Capalith is refused with a plain sentence that says so, and nothing else changes.

The one relaxation of the blanket setup-error redaction: for this one cause the message is fixed text with two numbers (the
catalog's format and the newest format this version reads); it holds no path, source or setting. Every other failure keeps
its text.
"""
from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import TextContent

from capalith import store as store_module
from capalith.cli import main as cli_main
from capalith.onboarding import prepare_catalog
from capalith.store import Store, StoreError
from tests.helpers import make_skill

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NEWER = store_module.SCHEMA_VERSION + 1
MESSAGE = (
    f"catalog_newer: this catalog was written by a newer Capalith (catalog format {NEWER}; "
    f"this version reads up to {store_module.SCHEMA_VERSION}). Upgrade Capalith, or point at another catalog."
)


def set_version(database: Path, version: int) -> None:
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f"PRAGMA user_version={version}")


def newer_database(temp: Path) -> Path:
    database = temp / "newer.sqlite3"
    Store(database).initialize()
    set_version(database, NEWER)
    return database


def managed_catalog(temp: Path, root: Path) -> Path:
    """The catalog a managed server would open for this root, made by this code and then marked newer."""
    with patch.dict(os.environ, {"XDG_DATA_HOME": str(temp / "data")}):
        database = prepare_catalog([root])
    set_version(database, NEWER)
    return database


async def call(parameters: StdioServerParameters, tool: str, arguments: dict) -> tuple[bool, str]:
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            content = result.content[0]
            assert isinstance(content, TextContent)
            return bool(result.is_error), content.text


class NewerCatalogTests(unittest.TestCase):
    def test_initialize_says_the_catalog_is_newer_in_plain_words_and_holds_no_path(self):
        with tempfile.TemporaryDirectory() as directory:
            database = newer_database(Path(directory))
            with self.assertRaises(StoreError) as caught:
                Store(database).initialize()
            self.assertEqual(MESSAGE, str(caught.exception))
            self.assertNotIn(directory, str(caught.exception))
            self.assertIsInstance(caught.exception, store_module.StoreSchemaNewer)

    def test_a_read_only_open_says_the_same(self):
        with tempfile.TemporaryDirectory() as directory:
            database = newer_database(Path(directory))
            with self.assertRaises(StoreError) as caught:
                Store(database, read_only=True)._connect()
            self.assertEqual(MESSAGE, str(caught.exception))

    def test_other_unsupported_versions_and_older_ones_keep_the_old_text(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "odd.sqlite3"
            Store(database).initialize()
            set_version(database, -1)
            with self.assertRaisesRegex(StoreError, r"^unsupported store schema version: -1$"):
                Store(database).initialize()
            older = Path(directory) / "older.sqlite3"
            Store(older).initialize()
            set_version(older, store_module.SCHEMA_VERSION - 1)
            with self.assertRaises(sqlite3.DatabaseError) as caught:
                Store(older, read_only=True)._connect()
            self.assertEqual("unsupported store schema version", str(caught.exception))

    def test_the_command_line_prints_the_sentence_and_exits_one(self):
        with tempfile.TemporaryDirectory() as directory:
            database = newer_database(Path(directory))
            out, err = io.StringIO(), io.StringIO()
            code = cli_main(["--db", str(database), "source", "list"], stdout=out, stderr=err)
            self.assertEqual(1, code)
            self.assertEqual(MESSAGE + "\n", err.getvalue())
            self.assertEqual("", out.getvalue())

    def test_a_db_server_answers_a_tool_call_with_the_sentence(self):
        with tempfile.TemporaryDirectory() as directory:
            database = newer_database(Path(directory))
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "capalith.mcp_server", "--db", str(database)], cwd=PROJECT_ROOT,
            )
            is_error, text = anyio.run(call, parameters, "discover", {"query": "anything"})
            self.assertTrue(is_error)
            self.assertEqual(MESSAGE, text)

    def test_a_managed_server_names_it_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            root = temp / "skills"
            make_skill(root, "demo-skill", b"demo")
            managed_catalog(temp, root)
            result = subprocess.run(
                [sys.executable, "-m", "capalith.mcp_server", "--source", str(root)],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
                env={**os.environ, "XDG_DATA_HOME": str(temp / "data")},
            )
            self.assertEqual(1, result.returncode)
            self.assertEqual(MESSAGE + "\n", result.stderr)
            self.assertNotIn(directory, result.stderr)

    def test_an_agent_managed_search_answers_with_the_sentence_and_an_unrelated_setup_failure_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            root = temp / "skills"
            make_skill(root, "demo-skill", b"demo")
            managed_catalog(temp, root)
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "capalith.mcp_server"], cwd=PROJECT_ROOT,
                env={"XDG_DATA_HOME": str(temp / "data"), "CAPALITH_SEMANTIC_MODEL": "/missing-capalith-test-model"},
            )
            is_error, text = anyio.run(call, parameters, "discover", {"query": "demo", "source_paths": [str(root)]})
            self.assertTrue(is_error)
            self.assertEqual(MESSAGE, text)
            is_error, text = anyio.run(call, parameters, "discover", {"query": "demo", "source_paths": [str(temp / "missing")]})
            self.assertTrue(is_error)
            self.assertEqual("catalog_setup_failed", text, "an unrelated setup failure says what it said before")


if __name__ == "__main__":
    unittest.main()
