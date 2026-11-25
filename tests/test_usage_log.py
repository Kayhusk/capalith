"""The local counts-only usage log (`usage.*` settings): off by default, one line per search, no text of the search.

Seams: a real stdio server answering `discover` (the log file it leaves), the `capalith usage` command (what it
prints from that file), and the settings. Nothing here reads a private function.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

from capalith import usage
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


@contextlib.contextmanager
def logging_to(folder: Path, retention_days: int = 30):
    """In this process, the log on and aimed at `folder`, through a user settings file."""
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "settings.toml"
        config.write_text(f'version = 1\n[usage]\nenabled = true\ndir = "{folder}"\nretention_days = {retention_days}\n')
        config.chmod(0o600)
        with patch.dict(os.environ, {"CAPALITH_CONFIG": str(config)}):
            yield


def result_with(reason: str | None, path: str = "free") -> dict:
    return {"ranking": {"path": path, "reason": reason}, "candidates": [{"name": "x"}]}


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

    # The four repairs of the first review (each fails on the first version of the log).

    def test_a_reason_that_is_not_a_known_code_is_written_as_other(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "log"
            with logging_to(folder):
                for reason in ("missing_key", "provider_timeout", "saffron", "a" * 80, "Not A Code", None):
                    usage.record_search("discover", time.perf_counter(), result_with(reason))
            written = [json.loads(line)["reason"] for line in (folder / "usage.jsonl").read_text().splitlines()]
            self.assertEqual(["missing_key", "provider_timeout", "other", "other", "other", None], written)

    def test_every_reason_the_code_can_give_is_a_known_code(self) -> None:
        """A drift guard: a new reason string in the source must be added to `usage.REASONS` on purpose."""
        used = set()
        for path in (PROJECT_ROOT / "capalith").glob("*.py"):
            used |= set(re.findall(r'RankingUnavailable\("([a-z_]+)"\)', path.read_text()))
        used |= set(re.findall(r'\blocal\("([a-z_]+)"\)', (PROJECT_ROOT / "capalith" / "store.py").read_text()))
        self.assertTrue(used)
        self.assertEqual(set(), used - usage.REASONS)

    def test_a_search_never_waits_for_the_log_and_every_lost_line_is_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "log"
            with logging_to(folder):
                usage.record_search("discover", time.perf_counter(), result_with("not_configured"))   # makes the folder
                import fcntl
                holder = os.open(folder / "usage.lock", os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(holder, fcntl.LOCK_EX)   # someone is rewriting the log
                log_path = folder / "usage.jsonl"   # an expired first line: a retention trim is due
                log_path.write_text(json.dumps(entry("2020-01-01", 5), separators=(",", ":")) + "\n" + log_path.read_text())
                threading.Timer(1.0, os.close, [holder]).start()   # the other party lets go after a second
                before = usage.failures()
                started = time.perf_counter()
                usage.record_search("discover", time.perf_counter(), result_with("not_configured"))
                waited = time.perf_counter() - started
            self.assertLess(waited, 0.5, "a search is never held up by the log")
            self.assertEqual(before + 1, usage.failures(), "the line that did not land is counted")
            current = [line for line in (folder / "usage.jsonl").read_text().splitlines() if not line.startswith('{"t":"2020')]
            self.assertEqual(1, len(current))
            time.sleep(1.1)

    def test_lines_that_land_plus_lines_counted_equal_the_searches_made_while_old_lines_expire(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "log"
            work = Workspace(Path(directory) / "w", usage="on")
            work.write_log([entry("2020-01-01", 5)] * 3)
            old = (work.folder / "usage.jsonl").read_text()
            folder.mkdir(mode=0o700)
            (folder / "usage.jsonl").write_text(old)
            (folder / "usage.jsonl").chmod(0o600)
            with logging_to(folder):
                before = usage.failures()
                threads = [threading.Thread(target=lambda: [usage.record_search("discover", time.perf_counter(), result_with("not_configured"))
                                                           for _ in range(40)]) for _ in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                lost = usage.failures() - before
            current = [line for line in (folder / "usage.jsonl").read_text().splitlines() if not line.startswith('{"t":"2020')]
            self.assertEqual(160, len(current) + lost, "no line vanishes without being counted")

    def test_a_folder_or_file_that_is_a_symbolic_link_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "elsewhere"
            target.mkdir(mode=0o700)
            link = Path(directory) / "link"
            link.symlink_to(target)
            with logging_to(link):
                before = usage.failures()
                usage.record_search("discover", time.perf_counter(), result_with("not_configured"))
            self.assertEqual([], list(target.iterdir()), "nothing is created behind the link")
            self.assertEqual(before + 1, usage.failures())
            real = Path(directory) / "real"
            real.mkdir(mode=0o700)
            (real / "usage.jsonl").symlink_to(target / "stolen.txt")
            with logging_to(real):
                usage.record_search("discover", time.perf_counter(), result_with("not_configured"))
            self.assertEqual([], list(target.iterdir()), "nothing is written through a linked log file")

    def test_a_command_line_search_that_fails_is_counted_like_an_mcp_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Workspace(Path(directory), usage="on")
            done = work.command("--db", str(Path(directory) / "missing.sqlite3"), "discover", "secret-needle words")
            self.assertEqual(1, done.returncode)
            line = work.lines()[0]
            self.assertEqual(("cli", "error", "database_unavailable", "none"), (line["tool"], line["status"], line["error"], line["path"]))
            self.assertNotIn("needle", (work.folder / "usage.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
