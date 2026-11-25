"""The local counts-only usage log (`usage.*` settings): off by default, one line per search, no text of the search.

Seams: a real stdio server answering `discover` (the log file it leaves), the `capalith usage` command (what it
prints from that file), and the settings. Nothing here reads a private function.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

from capalith.intake import scan_local_source
from capalith.settings import defaults
from capalith.store import Store
from tests.helpers import make_skill

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUERY = "zebra-giraffe quarterly invoice reconciliation"
SECRET_SKILL_TEXT = "hippopotamus-ledger-passphrase"
FIELDS = {"t", "tool", "path", "reason", "n", "status", "ms", "error", "id"}


class Workspace:
    """A catalog with two skills, a settings file and a usage folder, all inside one temporary folder."""

    def __init__(self, temp: Path, usage: str = "", extra_settings: str = "") -> None:
        self.temp = temp
        self.folder = temp / "usage-log"
        source_root = temp / "source"
        for name, text in (("invoice-helper", QUERY), ("zebra-care", SECRET_SKILL_TEXT)):
            bundle = make_skill(source_root, name)
            (bundle / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Helps with {text}.\n---\n\n# {name}\n\n{text}\n", encoding="utf-8")
        self.database = temp / "capalith.sqlite3"
        store = Store(self.database)
        store.initialize()
        source = store.add_local_source(source_root)
        scan_local_source(store, source.id)
        self.config = temp / "settings.toml"
        if usage == "on":
            usage = f'[usage]\nenabled = true\ndir = "{self.folder}"\n'
        self.config.write_text("version = 1\n" + usage + extra_settings)
        self.config.chmod(0o600)

    def environment(self) -> dict[str, str]:
        return {**os.environ, "CAPALITH_CONFIG": str(self.config), "XDG_DATA_HOME": str(self.temp / "data"),
                "XDG_CACHE_HOME": str(self.temp / "cache"), "HOME": str(self.temp / "home")}

    def discover(self, *queries: str) -> list[bool]:
        """Run the stdio server on the catalog, send each query, return whether each answer was an error."""
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "capalith.mcp_server", "--db", str(self.database)],
            cwd=PROJECT_ROOT, env=self.environment())
        failed: list[bool] = []

        async def run() -> None:
            async with stdio_client(parameters) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    for query in queries:
                        failed.append(bool((await session.call_tool("discover", {"query": query})).is_error))

        anyio.run(run)
        return failed

    def lines(self) -> list[dict]:
        log = self.folder / "usage.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def command(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "capalith", *arguments], cwd=PROJECT_ROOT, env=self.environment(),
            capture_output=True, text=True, timeout=120)

    def call(self, name: str, arguments: dict) -> bool:
        """One raw tool call on the stdio server; whether it was an error."""
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "capalith.mcp_server", "--db", str(self.database)],
            cwd=PROJECT_ROOT, env=self.environment())
        failed: list[bool] = []

        async def run() -> None:
            async with stdio_client(parameters) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    failed.append(bool((await session.call_tool(name, arguments)).is_error))

        anyio.run(run)
        return failed[0]

    def structured(self, *calls: tuple[str, dict]) -> list[dict]:
        """Several tool calls in one stdio session; each call's structured answer."""
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "capalith.mcp_server", "--db", str(self.database)],
            cwd=PROJECT_ROOT, env=self.environment())
        answers: list[dict] = []

        async def run() -> None:
            async with stdio_client(parameters) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    for name, arguments in calls:
                        answers.append((await session.call_tool(name, arguments)).structured_content or {})

        anyio.run(run)
        return answers

    def write_log(self, lines: list[dict]) -> None:
        self.folder.mkdir(mode=0o700, exist_ok=True)
        log = self.folder / "usage.jsonl"
        log.write_text("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines))
        log.chmod(0o600)


def entry(day: str, ms: int, path: str = "free", reason: str | None = "not_configured", error: str = "none") -> dict:
    return {"t": f"{day}T10:00:00Z", "tool": "discover", "path": path, "reason": reason, "n": 3, "status": "possible",
            "ms": ms, "error": error, "id": "0123456789abcdef"}


class UsageLogTests(unittest.TestCase):
    def test_off_by_default_and_a_search_leaves_no_trace(self) -> None:
        self.assertFalse(defaults()["usage"]["enabled"])
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage=f'[usage]\ndir = "{Path(directory) / "usage-log"}"\n')
            self.assertEqual([False], work.discover(QUERY))
            self.assertFalse(work.folder.exists())

    def test_one_line_per_search_with_counts_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage="on")
            self.assertEqual([False, False], work.discover(QUERY, "no such thing at all xyzzy"))
            lines = work.lines()
            self.assertEqual(2, len(lines))
            for line in lines:
                self.assertEqual(FIELDS, set(line))
                self.assertEqual("discover", line["tool"])
                self.assertEqual("free", line["path"])
                self.assertEqual("none", line["error"])
                self.assertRegex(line["t"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
                self.assertIsInstance(line["ms"], int)
                self.assertRegex(line["id"], r"^[0-9a-f]{16}$")
            self.assertGreaterEqual(lines[0]["n"], 1)
            self.assertEqual(lines[0]["id"], lines[1]["id"], "one random id per install")
            # Owner only: the folder and the files.
            self.assertEqual(0o700, stat.S_IMODE(work.folder.stat().st_mode))
            for entry in work.folder.iterdir():
                self.assertEqual(0o600, stat.S_IMODE(entry.stat().st_mode), entry.name)

    def test_no_query_text_skill_text_path_or_account_reaches_the_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage="on")
            work.discover(QUERY, "invoice")
            self.assertEqual(2, len(work.lines()))
            forbidden = [*QUERY.split(), QUERY, SECRET_SKILL_TEXT, "invoice-helper", "zebra-care", directory,
                         str(PROJECT_ROOT), os.path.expanduser("~"), os.environ.get("USER", "\0"), os.uname().nodename,
                         "Helps with", "SKILL.md"]
            files = [path for path in work.folder.rglob("*") if path.is_file()]
            self.assertTrue(files)
            for path in files:
                text = path.read_text()
                for word in forbidden:
                    self.assertNotIn(word, text, f"{word!r} reached {path.name}")

    def test_skill_names_are_added_only_when_the_owner_turns_them_on(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage="on")
            work.config.write_text(f'version = 1\n[usage]\nenabled = true\nskill_names = true\ndir = "{work.folder}"\n')
            work.discover(QUERY)
            line = work.lines()[0]
            self.assertEqual(FIELDS | {"names"}, set(line))
            self.assertIn("invoice-helper", line["names"])
            self.assertNotIn(QUERY, (work.folder / "usage.jsonl").read_text())

    def test_a_failed_search_is_a_line_with_its_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage="on")
            self.assertTrue(work.call("discover", {"query": QUERY, "unexpected": True}))
            line = work.lines()[0]
            self.assertEqual(("invalid_request", "error", "none", 0), (line["error"], line["status"], line["path"], line["n"]))
            self.assertNotIn(QUERY, (work.folder / "usage.jsonl").read_text())

    def test_the_command_line_search_is_counted_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage="on")
            done = work.command("--db", str(work.database), "discover", "invoice")
            self.assertEqual(0, done.returncode, done.stderr)
            self.assertEqual(["cli"], [line["tool"] for line in work.lines()])

    def test_a_log_that_cannot_be_written_never_fails_the_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage="on")
            work.folder.write_text("a file where the folder should be")
            self.assertEqual([False], work.discover(QUERY))

    def test_swallowed_write_failures_are_counted_where_the_owner_can_see_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage="on")
            work.folder.write_text("a file where the folder should be")
            _, report = work.structured(("discover", {"query": QUERY}), ("discover", {"query": "invoice"}), ("config_show", {}))[1:]
            self.assertEqual({"enabled": True, "write_failures": 2}, report.get("usage"), "one count per swallowed write")
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory))   # off: the report has no usage key at all
            self.assertNotIn("usage", work.structured(("config_show", {}))[0])

    def test_old_lines_are_removed_when_a_new_one_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage="on")
            work.write_log([entry("2020-01-01", 5), entry("2020-01-02", 6)])
            work.discover(QUERY)
            lines = work.lines()
            self.assertEqual(1, len(lines))
            self.assertGreater(lines[0]["t"], "2026")

    def test_the_summary_is_one_line_per_day_and_reads_only_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage="on")
            work.write_log([entry("2026-11-24", ms) for ms in (10, 20, 30, 40)]
                           + [entry("2026-11-25", 100, path="ai", reason=None),
                              entry("2026-11-25", 300, reason="missing_key"),
                              entry("2026-11-25", 200, path="none", reason=None, error="catalog_setup_failed")])
            plain = work.command("usage")
            self.assertEqual(0, plain.returncode, plain.stderr)
            self.assertEqual([
                "2026-11-24  searches 4  ai 0  free 4 (not_configured 4)  errors 0  p50 20 ms  p95 40 ms",
                "2026-11-25  searches 3  ai 1  free 1 (missing_key 1)  errors 1  p50 200 ms  p95 300 ms",
            ], plain.stdout.splitlines())
            data = json.loads(work.command("usage", "--json").stdout)
            self.assertEqual(["2026-11-24", "2026-11-25"], [day["day"] for day in data["days"]])
            self.assertEqual({"day": "2026-11-25", "searches": 3, "ai": 1, "free": 1, "free_reasons": {"missing_key": 1},
                              "errors": 1, "p50_ms": 200, "p95_ms": 300}, data["days"][1])

    def test_the_summary_with_no_log_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage="on")
            done = work.command("usage")
            self.assertEqual((0, "no searches logged"), (done.returncode, done.stdout.strip()))


if __name__ == "__main__":
    unittest.main()
