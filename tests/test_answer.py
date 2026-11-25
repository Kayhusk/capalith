from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

from capalith import answer
from capalith.intake import scan_git_source
from capalith.store import Store
from capalith.settings import SettingsError, _validate_answer, defaults

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = defaults()["answer"]


def candidate(name, *, description="Does a task.", words=("task",), similarity=None, name_hits=0,
              description_hits=1, source="s1", bundle=None, relationships=()):
    score = {"name": name_hits, "description": description_hits, "distinct_terms": len(words)}
    if similarity is not None:
        score["semantic_similarity"] = similarity
    return {
        "name": name, "description": description, "matched_terms": list(words), "score": score,
        "source_id": source, "bundle_path": bundle or name, "artifact_id": "art_" + name,
        "content_digest": "sha256:" + "0" * 64, "relationships": list(relationships), "matches": [],
    }


def result(candidates, *, query="a task", semantic="used", more=False):
    return {
        "candidates": candidates, "query_terms": query.split(),
        "semantic": {"status": semantic},
        "retrieval": {"method": "m", "limit": 3, "offset": 0, "returned": len(candidates),
                      "has_more": more, "next_offset": 3, "view_id": "d1:" + "a" * 64},
    }


class FakeStore:
    def __init__(self, sources, found=None):
        self._sources, self._found, self.asked = sources, found or {}, []

    def list_sources(self):
        return self._sources

    def lookup_name(self, name, *, source_ids=None):
        self.asked.append((name, source_ids))
        return self._found.get(name)


class JudgeTests(unittest.TestCase):
    def test_each_status_comes_from_today_s_signals_in_one_function(self):
        meaning = candidate("pdf-filler", similarity=0.8)
        self.assertEqual("exact", answer.judge(result([candidate("pdf-filler")], query="pdf filler"), CONFIG)[0])
        self.assertEqual("possible", answer.judge(result([meaning]), CONFIG)[0])
        self.assertEqual("no_match", answer.judge(result([]), CONFIG)[0])

    def test_a_name_or_description_hit_is_possible_never_weak_or_close(self):
        # words in the name or description are some evidence, with or without the meaning side; "good" needs a judge (test_honest_status)
        words_only = candidate("pdf-filler")
        self.assertEqual("possible", answer.judge(result([words_only], semantic="no_match"), CONFIG)[0])
        by_name = candidate("pdf-filler", name_hits=1, description_hits=0)
        self.assertEqual("possible", answer.judge(result([by_name], semantic="no_match"), CONFIG)[0])
        tie = [candidate("a-one", similarity=0.80), candidate("b-two", similarity=0.80)]
        self.assertEqual("possible", answer.judge(result(tie), CONFIG)[0])

    def test_weak_is_for_body_only_or_meaning_only_and_says_which(self):
        body_only = candidate("pdf-filler", name_hits=0, description_hits=0)
        status, signal = answer.judge(result([body_only], semantic="no_match"), CONFIG)
        self.assertEqual("weak", status)  # a body hit relates: never the absolute no_match
        self.assertIn("body", signal)
        meaning_only = candidate("pdf-filler", name_hits=0, description_hits=0, similarity=0.8)
        status, signal = answer.judge(result([meaning_only]), CONFIG)
        self.assertEqual("weak", status)
        self.assertIn("meaning", signal)

    def test_close_needs_the_absolute_gap_inside_the_margin_and_no_word_hit(self):
        def bare(name, similarity):
            return candidate(name, similarity=similarity, name_hits=0, description_hits=0)
        first, near, far = bare("a-one", 0.80), bare("b-two", 0.79), bare("b-two", 0.70)
        self.assertEqual("close", answer.judge(result([first, near]), CONFIG)[0])
        self.assertEqual("weak", answer.judge(result([first, far]), CONFIG)[0])
        # the reviewed case: a low first score and a much higher second one is not close
        low_first = [bare("a-one", 0.67), bare("b-two", 0.97)]
        self.assertNotEqual("close", answer.judge(result(low_first), CONFIG)[0])
        # and a near tie behind a first place with a name or description hit stays good
        words = [candidate("a-one", similarity=0.80), candidate("b-two", similarity=0.79)]
        self.assertEqual("possible", answer.judge(result(words), CONFIG)[0])


class ShapeTests(unittest.TestCase):
    def test_full_detail_returns_the_record_unchanged(self):
        record = result([candidate("pdf-filler", similarity=0.8)])
        self.assertIs(record, answer.shape(FakeStore([]), record, "full"))

    def test_read_path_is_absolute_for_local_and_an_inspect_call_otherwise(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "pdf-filler").mkdir()
            (root / "pdf-filler/SKILL.md").write_text("---\nname: pdf-filler\n---\n")
            sources = [SimpleNamespace(id="s1", kind="local", locator=str(root)),
                       SimpleNamespace(id="s2", kind="git", locator="https://example.com/x.git"),
                       SimpleNamespace(id="s3", kind="local", locator=str(root))]
            record = result([
                candidate("pdf-filler", similarity=0.8, source="s1"),
                candidate("remote-skill", similarity=0.7, source="s2"),
                candidate("vanished", similarity=0.6, source="s3"),
            ])
            shaped = answer.shape(FakeStore(sources), record, "short")
            local, remote, gone = shaped["candidates"]
            self.assertEqual(str(root / "pdf-filler/SKILL.md"), local["read"])
            self.assertTrue(Path(local["read"]).is_absolute())
            for entry in (remote, gone):
                self.assertNotIn("read", entry)
                self.assertEqual("SKILL.md", entry["inspect"]["resource_path"])
            self.assertIn("read: " + local["read"], shaped["_text"])
            self.assertIn("no local file; inspect art_remote-skill", shaped["_text"])

    def test_short_text_stays_inside_the_budget_without_dropping_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            names = [f"skill-{index}" for index in range(3)]
            for name in names:
                (root / name).mkdir()
                (root / name / "SKILL.md").write_text("x")
            long = "Keep every animation inside the frame budget and off the main thread " * 5
            record = result([candidate(name, description=long, similarity=0.9 - index * 0.1,
                                       words=("animation", "easing", "modal", "opening"))
                             for index, name in enumerate(names)], more=True)
            store = FakeStore([SimpleNamespace(id="s1", kind="local", locator=str(root))])
            roomy = answer.shape(store, copy.deepcopy(record), "short")
            tight = {"answer": {**CONFIG, "max_chars": len(roomy["_text"]) - 60}}
            with patch.object(answer, "load_settings", return_value=tight):
                shaped = answer.shape(store, record, "short")
            self.assertLessEqual(len(shaped["_text"]), tight["answer"]["max_chars"])
            self.assertLess(len(shaped["candidates"][0]["summary"]), len(roomy["candidates"][0]["summary"]))
            self.assertEqual(names, [entry["name"] for entry in shaped["candidates"]])
            self.assertEqual({"offset": 3, "view_id": "d1:" + "a" * 64}, shaped["more"])

    def test_no_match_is_only_for_an_empty_result_and_says_so(self):
        shaped = answer.shape(FakeStore([]), result([], semantic="no_match"), "short")
        self.assertEqual("no_match", shaped["status"])
        self.assertTrue(shaped["_text"].startswith("no_match: No skill in the catalog fits this task."))
        self.assertEqual([], shaped["candidates"])

    def test_a_body_only_hit_is_weak_with_its_reason_and_still_listed(self):
        body_only = candidate("design-taste", name_hits=0, description_hits=0)
        shaped = answer.shape(FakeStore([]), result([body_only], semantic="no_match"), "short")
        self.assertEqual("weak", shaped["status"])
        self.assertNotIn("No skill in the catalog fits", shaped["_text"])
        self.assertNotIn("Don't load", shaped["_text"])
        self.assertIn("body", shaped["_text"].splitlines()[0])
        self.assertEqual(["design-taste"], [entry["name"] for entry in shaped["candidates"]])

    def test_a_for_x_use_y_sentence_pulls_the_named_skill_in_with_its_reason(self):
        redirector = candidate("animation-performance", similarity=0.8, words=("animation", "easing"),
                               description="Keep animations fast. For choosing easing or duration use animate.")
        target = candidate("animate", similarity=0.5, description="Build an animation from scratch.")
        store = FakeStore([], {"animate": target})
        shaped = answer.shape(store, result([redirector], query="tune easing of a modal"), "short")
        self.assertEqual("for choosing easing or duration use animate", shaped["candidates"][0]["instead"])
        extra = shaped["candidates"][1]
        self.assertEqual(("animate", "redirect", "named by animation-performance"),
                         (extra["name"], extra["via"], extra["why"]))
        self.assertEqual([("animate", None)], store.asked)
        # the redirect is not about this task, or the switch is off
        other = answer.shape(FakeStore([], {"animate": target}), result([redirector], query="database index"), "short")
        self.assertEqual(1, len(other["candidates"]))
        self.assertNotIn("instead", other["candidates"][0])

    def test_a_redirect_to_a_skill_that_is_not_in_the_searched_set_is_not_shown(self):
        redirector = candidate("animation-performance", similarity=0.8, words=("animation", "easing"),
                               description="Keep animations fast. For choosing easing or duration use animate.")
        missing = answer.shape(FakeStore([]), result([redirector], query="tune easing of a modal"), "short")
        self.assertEqual(1, len(missing["candidates"]))
        self.assertNotIn("instead", missing["candidates"][0])
        self.assertNotIn("instead", missing["_text"])
        self.assertNotIn("animate", missing["_text"].replace("animation-performance", ""))
        # the lookup is by exact name inside the searched sources only, never a ranked search
        store = FakeStore([], {"animate": candidate("animate", description="Build an animation.")})
        answer.shape(store, result([redirector], query="tune easing of a modal"), "short", ("s9",))
        self.assertEqual([("animate", ("s9",))], store.asked)
        # a target already on the page needs no lookup and is still shown
        page = [redirector, candidate("animate", similarity=0.5)]
        both = answer.shape(FakeStore([]), result(page, query="tune easing of a modal"), "short")
        self.assertEqual("for choosing easing or duration use animate", both["candidates"][0]["instead"])

    def test_messages_cannot_reach_attributes_or_unknown_fields(self):
        for bad in ("{name.__class__}", "{other}", "{name!r}", "{name:>9}", "{0}", "{"):
            config = copy.deepcopy(defaults()["answer"])
            config["messages"]["weak"] = bad
            with self.assertRaises(SettingsError, msg=bad):
                _validate_answer(config)
        config = copy.deepcopy(defaults()["answer"])
        config["detail"] = "verbose"
        with self.assertRaises(SettingsError):
            _validate_answer(config)


def local_source(root):
    return [SimpleNamespace(id="s1", kind="local", locator=str(root))]


class ReadPathBoundaryTests(unittest.TestCase):
    def test_a_symlink_made_after_indexing_never_yields_a_path_outside_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, outside = root / "source", root / "outside"
            for name in ("file-link", "dir-link", "inside-link", "plain"):
                (source / name).mkdir(parents=True)
            outside.mkdir()
            (outside / "SKILL.md").write_text("secret")
            (source / "plain/SKILL.md").write_text("ok")
            (source / "file-link/SKILL.md").symlink_to(outside / "SKILL.md")
            (source / "dir-link").rmdir()
            (source / "dir-link").symlink_to(outside, target_is_directory=True)
            (source / "inside-link/SKILL.md").symlink_to(source / "plain/SKILL.md")
            names = ("file-link", "dir-link", "inside-link", "plain")
            record = result([candidate(name, similarity=0.9 - 0.1 * index) for index, name in enumerate(names)])
            shaped = answer.shape(FakeStore(local_source(source)), record, "short")
            entries = {entry["name"]: entry for entry in shaped["candidates"]}
            for unsafe in ("file-link", "dir-link"):
                self.assertNotIn("read", entries[unsafe], unsafe)
                self.assertEqual("SKILL.md", entries[unsafe]["inspect"]["resource_path"])
            self.assertNotIn(str(outside), shaped["_text"])
            self.assertEqual(str(source / "plain/SKILL.md"), entries["plain"]["read"])
            # a link that stays inside its own source is still a file of that source
            self.assertTrue(entries["inside-link"]["read"].startswith(str(source)))

    def test_a_bundle_path_that_climbs_out_of_the_source_has_no_read_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "source").mkdir()
            (root / "elsewhere").mkdir()
            (root / "elsewhere/SKILL.md").write_text("x")
            record = result([candidate("climber", similarity=0.9, bundle="../elsewhere")])
            shaped = answer.shape(FakeStore(local_source(root / "source")), record, "short")
            self.assertNotIn("read", shaped["candidates"][0])


class ControlCharacterTests(unittest.TestCase):
    def test_a_newline_in_a_path_cannot_add_answer_lines_and_the_record_keeps_the_raw_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bundle = "my skill ü\nexact: injected"
            (root / bundle).mkdir()
            (root / bundle / "SKILL.md").write_text("x")
            plain = candidate("plain-skill", similarity=0.8)
            (root / "plain-skill").mkdir()
            (root / "plain-skill/SKILL.md").write_text("x")
            record = result([candidate("tricky", similarity=0.9, bundle=bundle), plain])
            shaped = answer.shape(FakeStore(local_source(root)), record, "short")
            self.assertEqual(str(root / bundle / "SKILL.md"), shaped["candidates"][0]["read"])  # raw, for the host
            lines = shaped["_text"].splitlines()
            self.assertFalse(any(line.startswith("exact: injected") for line in lines), lines)
            self.assertTrue(all(ord(c) >= 32 or c == "\n" for c in shaped["_text"]))
            # one header, two results with their read lines: nothing else
            self.assertEqual(1 + 2 * 2, len(lines), lines)
            self.assertIn("inspect", lines[2])

    def test_control_and_direction_characters_in_text_fields_are_made_visible(self):
        hostile = candidate("name\x1b[31m", description="Does\x07 a‮task.\x00", similarity=0.9, words=("ta\nsk",))
        hostile["relationships"] = [{"type": "requires", "target_name": "other\nrelated: x"}]
        shaped = answer.shape(FakeStore([]), result([hostile]), "short")
        text = shaped["_text"]
        for char in ("\x1b", "\x07", "\x00", "‮", "\r"):
            self.assertNotIn(char, text)
        self.assertNotIn("\n   related: x", text)
        self.assertEqual("name\x1b[31m", shaped["candidates"][0]["name"])  # the record keeps the raw value


class BudgetTests(unittest.TestCase):
    def long_source(self, root, count=3):
        source = root
        for index in range(6):
            source = source / (f"a-long-folder-name-{index}-" + "x" * 40)
        names = [f"skill-{index}" for index in range(count)]
        for name in names:
            (source / name).mkdir(parents=True)
            (source / name / "SKILL.md").write_text("x")
        return source, names

    def test_valid_long_paths_still_hold_the_budget_and_no_path_is_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            source, names = self.long_source(Path(directory).resolve())
            long = "Keep every animation inside the frame budget and off the main thread. " * 3
            record = result([candidate(name, description=long, similarity=0.9 - 0.1 * index,
                                       words=("animation", "easing", "modal"))
                             for index, name in enumerate(names)], more=True)
            shaped = answer.shape(FakeStore(local_source(source)), record, "short")
            text = shaped["_text"]
            self.assertGreater(sum(len(entry["read"]) for entry in shaped["candidates"]), CONFIG["max_chars"] // 2)
            self.assertLessEqual(len(text), CONFIG["max_chars"])
            self.assertEqual(names, [entry["name"] for entry in shaped["candidates"]])
            self.assertIn("path too long", text)
            for line in text.splitlines():
                if line.strip().startswith("read: /"):
                    self.assertTrue(any(line.strip() == "read: " + entry["read"] for entry in shaped["candidates"]), line)
            self.assertIn("more: offset", text)

    def test_when_nothing_else_fits_the_list_is_shortened_and_says_how_many_are_left_out(self):
        with tempfile.TemporaryDirectory() as directory:
            source, names = self.long_source(Path(directory).resolve(), count=6)
            record = result([candidate(name, similarity=0.9 - 0.05 * index) for index, name in enumerate(names)])
            tight = {"answer": {**CONFIG, "max_chars": 300}}
            with patch.object(answer, "load_settings", return_value=tight):
                shaped = answer.shape(FakeStore(local_source(source)), record, "short")
            self.assertLessEqual(len(shaped["_text"]), 300)
            self.assertEqual(names, [entry["name"] for entry in shaped["candidates"]])  # the record is whole
            self.assertRegex(shaped["_text"], r"\d+ more in the structured result")


class GitSourceTests(unittest.TestCase):
    def test_a_git_skill_names_one_inspect_call_that_returns_its_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = root / "repository"
            (repository / "ledger-skill").mkdir(parents=True)
            (repository / "ledger-skill/SKILL.md").write_text(
                "---\nname: ledger-skill\ndescription: Rotate the quarterly zebra ledger.\n---\n# Instructions\nRotate the ledger.\n")
            environment = {"PATH": os.environ["PATH"], "HOME": str(root), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
            for command in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "first"]):
                subprocess.run(["git", *command], cwd=repository, env=environment, check=True, capture_output=True)
            database = Store(root / "catalog.sqlite3")
            database.initialize()
            scan_git_source(database, database.add_git_source(repository.as_uri(), "refs/heads/main").id)
            store = Store(root / "catalog.sqlite3", read_only=True)
            shaped = answer.shape(store, store.discover("quarterly zebra ledger", limit=3), "short")
            entry = shaped["candidates"][0]
            self.assertEqual("ledger-skill", entry["name"])
            self.assertNotIn("read", entry)
            stored = store.get_artifact(entry["inspect"]["artifact_id"], entry["inspect"]["content_digest"], resource_path="SKILL.md")
            self.assertIn("Rotate the ledger.", json.dumps(stored))
            self.assertIn("no local file; inspect " + entry["inspect"]["artifact_id"], shaped["_text"])


def skill(root: Path, name: str, description: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n# Instructions\n{description}\n")


class AnswerThroughMcpTests(unittest.TestCase):
    def test_short_answer_full_record_setting_and_no_match_through_the_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("home", "config", "data", "cache", "work"):
                (root / name).mkdir()
            sources = root / "home/.claude/skills"
            skill(sources, "pdf-form-filler", "Fill in and flatten PDF forms. For merging pdf files use pdf-merger.")
            skill(sources, "pdf-merger", "Merge several PDF files into one document.")
            skill(sources, "spreadsheet-pivot", "Build pivot tables from spreadsheet data.")
            environment = {"PATH": os.environ["PATH"], "HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "config"),
                           "XDG_DATA_HOME": str(root / "data"), "XDG_CACHE_HOME": str(root / "cache")}
            config = root / "config/capalith/settings.toml"

            async def exercise():
                params = StdioServerParameters(command=sys.executable, args=["-m", "capalith.mcp_server"],
                                               cwd=PROJECT_ROOT, env=environment)
                async with stdio_client(params) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                        description = tools["discover"].description
                        self.assertTrue(description.startswith("Find the skill for the task you are about to start."))
                        self.assertIn("Don't call it to install a skill", description)
                        self.assertEqual(["full", "short"], sorted(tools["discover"].input_schema["properties"]["detail"]["enum"]))

                        async def discover(query, **extra):
                            response = await session.call_tool("discover", {"query": query, "workspace": str(root / "work"), **extra})
                            self.assertFalse(response.is_error, response)
                            return response

                        short = await discover("fill in a pdf form merging pdf files", limit=3)
                        text = short.content[0].text
                        self.assertLess(len(text), 1200)
                        self.assertEqual("pdf-form-filler", short.structured_content["candidates"][0]["name"])
                        expected = str(sources / "pdf-form-filler/SKILL.md")
                        self.assertIn("read: " + expected, text)
                        self.assertEqual(expected, short.structured_content["candidates"][0]["read"])
                        self.assertNotIn("artifact_id", short.structured_content["candidates"][0])
                        self.assertEqual("short", short.structured_content["detail"])

                        full = await discover("fill in a pdf form", detail="full", limit=3)
                        record = full.structured_content["candidates"][0]
                        self.assertIn("artifact_id", record)
                        self.assertIn('"candidates"', full.content[0].text)

                        nothing = await discover("quantum zebra marmalade")
                        self.assertEqual("no_match", nothing.structured_content["status"])
                        self.assertIn("No skill in the catalog fits this task", nothing.content[0].text)

                        config.parent.mkdir(mode=0o700, exist_ok=True)
                        config.write_text('[answer]\ndetail = "full"\n')
                        config.chmod(0o600)
                        default_full = await discover("fill in a pdf form", limit=1)
                        self.assertIn("artifact_id", default_full.structured_content["candidates"][0])
                        config.write_text('[answer]\ndetail = "short"\nsummary_chars = 30\nmax_redirects = 0\n')
                        config.chmod(0o600)
                        narrow = await discover("fill in a pdf form merging pdf files", limit=3)
                        self.assertNotIn("redirect", narrow.content[0].text)
                        self.assertTrue(all(len(entry["summary"]) <= 30 for entry in narrow.structured_content["candidates"]))

            anyio.run(exercise)

    def test_the_short_answer_lists_three_by_default_and_the_limit_and_settings_still_change_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("home", "config", "data", "cache", "work"):
                (root / name).mkdir()
            sources = root / "home/.claude/skills"
            for index in range(6):
                skill(sources, f"pdf-tool-{index}", f"Handle pdf documents, variant {index}.")
            environment = {"PATH": os.environ["PATH"], "HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "config"),
                           "XDG_DATA_HOME": str(root / "data"), "XDG_CACHE_HOME": str(root / "cache")}
            config = root / "config/capalith/settings.toml"

            async def exercise():
                params = StdioServerParameters(command=sys.executable, args=["-m", "capalith.mcp_server"],
                                               cwd=PROJECT_ROOT, env=environment)
                async with stdio_client(params) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                        self.assertEqual(3, tools["discover"].input_schema["properties"]["limit"]["default"])

                        async def count(**extra):
                            response = await session.call_tool("discover", {"query": "pdf documents", "workspace": str(root / "work"), **extra})
                            self.assertFalse(response.is_error, response)
                            record = response.structured_content
                            return record["retrieval"]["limit"], len(record["candidates"])

                        self.assertEqual((3, 3), await count())
                        self.assertEqual((5, 5), await count(limit=5))
                        full = await session.call_tool("discover", {"query": "pdf documents", "workspace": str(root / "work"), "detail": "full"})
                        self.assertEqual(5, full.structured_content["retrieval"]["limit"])  # the full record keeps today's page
                        config.parent.mkdir(mode=0o700, exist_ok=True)
                        config.write_text("[answer]\ndefault_limit = 2\n")
                        config.chmod(0o600)
                        self.assertEqual((2, 2), await count())

            anyio.run(exercise)


if __name__ == "__main__":
    unittest.main()
