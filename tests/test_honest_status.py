"""The free search's status says only what it knows, and three newcomer errors.

Measured on the development sets: with the earlier rule "good" was on a right first result 30 times, a wrong one
24 times and a no-answer case 25 times; no rule that needs only words or only the shipped gate reaches 8 right
in 10, so "good" is shown only where something judged it.
"""
from __future__ import annotations

import copy
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

from capalith import answer, mcp_server, semantic
from capalith.settings import SettingsError, _validate_answer, defaults, load_settings
from tests.test_answer import CONFIG, FakeStore, candidate, result, skill

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def with_good_from(value):
    return {**CONFIG, "good_from": value}


class StatusTests(unittest.TestCase):
    def test_a_name_or_description_word_is_only_possible_even_with_a_strong_meaning_score(self):
        for similarity in (None, 0.66, 0.9):
            record = result([candidate("pdf-filler", similarity=similarity)])
            self.assertEqual("possible", answer.judge(record, CONFIG)[0], similarity)

    def test_good_needs_the_ai_ranking_or_a_meaning_score_at_the_users_good_from(self):
        record = result([candidate("pdf-filler", similarity=0.70)])
        self.assertEqual("good", answer.judge(record, with_good_from(0.70))[0])
        self.assertEqual("possible", answer.judge(result([candidate("pdf-filler", similarity=0.69)]), with_good_from(0.70))[0])
        self.assertEqual("possible", answer.judge(result([candidate("pdf-filler", similarity=None)]), with_good_from(0.70))[0])
        # a high score without a word in the name or description is not good either
        bare = candidate("pdf-filler", similarity=0.95, name_hits=0, description_hits=0)
        self.assertEqual("weak", answer.judge(result([bare]), with_good_from(0.70))[0])
        # where the AI ranking chose, its answer stands
        ranked = result([candidate("pdf-filler", similarity=None)])
        ranked["ranking"] = {"path": "ai"}
        self.assertEqual("good", answer.judge(ranked, CONFIG)[0])
        # and when it found nothing there is no candidate: no match
        self.assertEqual("no_match", answer.judge({**result([]), "ranking": {"path": "ai", "abstained": True}}, CONFIG)[0])

    def test_exact_and_refusal_keep_their_place_before_any_of_it(self):
        self.assertEqual("exact", answer.judge(result([candidate("pdf-filler", similarity=0.1)], query="pdf filler"), CONFIG)[0])
        refusing = {**CONFIG, "refuse_below": 0.768}
        weak_meaning = result([candidate("pdf-filler")])
        weak_meaning["semantic"]["best_similarity"] = 0.5
        self.assertEqual("no_match", answer.judge(weak_meaning, refusing)[0])

    def test_the_setting_is_off_by_default_and_checked(self):
        self.assertEqual(0.0, defaults()["answer"]["good_from"])
        for bad in (-0.1, 1.5, True, "0.7", None, 1):
            config = copy.deepcopy(CONFIG)
            config["good_from"] = bad
            with self.assertRaises(SettingsError, msg=repr(bad)):
                _validate_answer(config)

    def test_a_common_word_in_a_description_is_not_a_possible_match(self):
        # Outsider run of 0.2.1: "book a dentist appointment" against a violin and a tide skill answered possible, on "a".
        violin = candidate("violin-tuning", description="Tune a violin by ear.", words=("a",), description_hits=1)
        record = result([violin], query="book a dentist appointment")
        self.assertNotEqual("possible", answer.judge(record, CONFIG)[0])
        self.assertNotEqual("good", answer.judge(record, CONFIG)[0])
        self.assertFalse(self.shaped_text(record).startswith("possible"))

    def test_a_distinctive_description_word_alongside_a_common_one_is_still_possible(self):
        booking = candidate("booking", description="Book a dentist appointment.", words=("book", "a", "dentist"), description_hits=3)
        self.assertEqual("possible", answer.judge(result([booking], query="book a dentist appointment"), CONFIG)[0])

    def test_a_skill_named_with_a_common_word_is_still_found_and_possible(self):
        named = candidate("how-to-commit", description="Writes commits.", words=("how",), name_hits=1, description_hits=0)
        self.assertEqual("possible", answer.judge(result([named], query="how is it done"), CONFIG)[0])
        bare = candidate("why", description="Explains reasons.", words=("why",), name_hits=1, description_hits=0)
        self.assertEqual("possible", answer.judge(result([bare], query="why did the build fail"), CONFIG)[0])

    def test_the_common_words_are_the_settings_list_and_an_empty_list_restores_the_old_rule(self):
        self.assertIn("a", defaults()["answer"]["hide_words"])
        violin = candidate("violin-tuning", description="Tune a violin by ear.", words=("a",), description_hits=1)
        record = result([violin], query="book a dentist appointment")
        self.assertEqual("possible", answer.judge(record, {**CONFIG, "hide_words": []})[0])

    def shaped_text(self, record):
        return answer.shape(FakeStore([]), record, "short")["_text"]


class WordsTests(unittest.TestCase):
    def shaped(self, candidates, **kwargs):
        return answer.shape(FakeStore([]), result(candidates, **kwargs), "short")

    def test_possible_weak_and_no_match_say_what_to_do_in_plain_words(self):
        possible = self.shaped([candidate("pdf-filler")])
        self.assertTrue(possible["_text"].startswith("possible: Possible match."), possible["_text"])
        weak = self.shaped([candidate("pdf-filler", name_hits=0, description_hits=0)])
        first = weak["_text"].splitlines()[0]
        self.assertIn("carry on without a skill", first)
        self.assertIn("body", first)
        self.assertNotIn("instructions text", first)
        nothing = self.shaped([])
        self.assertEqual(1, len(nothing["_text"].splitlines()), nothing["_text"])  # the maintainers' line is not the agent's
        self.assertTrue(nothing["_text"].endswith("Carry on without one."), nothing["_text"])
        self.assertEqual("nothing matched", nothing["signal"])  # the structured record keeps the rule's words for maintainers
        self.assertFalse(any("nearest in the structured" in line for line in nothing["_text"].splitlines()))

    def test_a_refused_search_is_one_line_for_the_agent_and_keeps_the_near_misses_in_the_record(self):
        record = result([candidate("pdf-filler"), candidate("other-skill")])
        record["semantic"]["best_similarity"] = 0.40
        with patch.object(answer, "load_settings", return_value={"answer": {**CONFIG, "refuse_below": 0.768}}):
            shaped = answer.shape(FakeStore([]), record, "short")
        self.assertEqual("no_match", shaped["status"])
        self.assertEqual(1, len(shaped["_text"].splitlines()), shaped["_text"])
        self.assertIn("pdf-filler", shaped["_text"])  # the nearest, named in the sentence
        self.assertEqual(["pdf-filler", "other-skill"], [entry["name"] for entry in shaped["candidates"]])
        self.assertIn("refuse_below", shaped["signal"])

    def test_the_tags_are_plain_words(self):
        both = self.shaped([candidate("pdf-filler", similarity=0.8, words=("pdf", "form"))])["candidates"][0]["why"]
        self.assertEqual("matched by meaning and your words 'pdf', 'form'", both)
        words = self.shaped([candidate("pdf-filler", words=("pdf",))])["candidates"][0]["why"]
        self.assertEqual("matched by your words 'pdf'", words)
        meaning = self.shaped([candidate("pdf-filler", similarity=0.8, words=(), name_hits=0, description_hits=0)])["candidates"][0]["why"]
        self.assertEqual("matched by meaning", meaning)
        bare = self.shaped([candidate("pdf-filler", words=())])["candidates"][0]["why"]
        self.assertEqual("matched by words only", bare)
        text = self.shaped([candidate("pdf-filler", similarity=0.8, words=("pdf", "form"))])["_text"]
        self.assertIn("(matched by meaning and your words 'pdf', 'form')", text)
        self.assertNotIn("[", text)

    def test_function_words_are_hidden(self):
        hidden = set(defaults()["answer"]["hide_words"])
        self.assertTrue({"up", "over", "who", "long", "on", "in", "to"} <= hidden)

    def test_discover_says_when_to_call_and_keeps_the_maintainers_text_with_the_arguments(self):
        for text in (mcp_server._AUTO_DISCOVER_TOOL.description, mcp_server._WHEN_TO_CALL):
            self.assertTrue(text.startswith("Find the skill for the task you are about to start."), text)
            self.assertIn("a short answer", text)
            for jargon in ("host_homes", "ranking transport", "snapshot", "TypeSafe"):
                self.assertNotIn(jargon, text)
        properties = mcp_server._AUTO_DISCOVER_TOOL.input_schema["properties"]
        self.assertIn("TypeSafe", mcp_server._INPUT_SCHEMAS["discover"]["properties"]["query"]["description"])
        self.assertIn("when it differs from the server", properties["workspace"]["description"])
        self.assertIn("provider", mcp_server._INPUT_SCHEMAS["discover"]["properties"]["query"]["description"])


class ErrorTests(unittest.TestCase):
    def settings_file(self, root: Path, *, folder_mode: int, file_mode: int) -> Path:
        folder = root / "config/capalith"
        folder.mkdir(parents=True)
        path = folder / "settings.toml"
        path.write_text("version = 1\n")
        os.chmod(root / "config", 0o700)
        os.chmod(folder, folder_mode)
        os.chmod(path, file_mode)
        return path

    def load(self, root: Path):
        with patch.dict(os.environ, {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")}, clear=True):
            return load_settings()

    def test_a_writable_folder_or_file_is_refused_and_the_error_says_which_path_and_the_one_chmod(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = self.settings_file(root, folder_mode=0o775, file_mode=0o600)
            with self.assertRaises(SettingsError) as raised:
                self.load(root)
            message = str(raised.exception)
            self.assertTrue(message.startswith("settings_untrusted: directory"), message)
            self.assertIn(str(path.parent), message)
            self.assertIn(f"chmod go-w {path.parent}", message)
            os.chmod(path.parent, 0o755)
            os.chmod(path, 0o664)
            with self.assertRaises(SettingsError) as raised:
                self.load(root)
            self.assertTrue(str(raised.exception).startswith("settings_untrusted: file"))
            self.assertIn(f"chmod go-w {path}", str(raised.exception))
            # the check itself is not loosened: the file is still refused until its mode is fixed
            os.chmod(path, 0o644)
            self.assertEqual(1, self.load(root)["version"])

    def test_a_link_has_no_chmod_in_its_message_but_says_why(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = self.settings_file(root, folder_mode=0o700, file_mode=0o600)
            saved = path.with_name("saved.toml")
            path.rename(saved)
            path.symlink_to(saved)
            with self.assertRaises(SettingsError) as raised:
                self.load(root)
            message = str(raised.exception)
            self.assertTrue(message.startswith("settings_untrusted: file"), message)
            self.assertIn(str(path), message)
            self.assertNotIn("chmod", message)
            self.assertIn("link", message)

    def test_no_skill_sources_says_where_it_looked_from_the_configured_hosts_and_what_to_do(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("home", "config/capalith", "data", "cache", "work"):
                (root / name).mkdir(parents=True)
            os.chmod(root / "config", 0o700)
            os.chmod(root / "config/capalith", 0o700)
            custom = root / "elsewhere"
            settings = root / "config/capalith/settings.toml"
            settings.write_text(f'version = 1\n[sources.hosts.claude]\nhome_default = "{custom}"\n')
            settings.chmod(0o600)
            environment = {"PATH": os.environ["PATH"], "HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "config"),
                           "XDG_DATA_HOME": str(root / "data"), "XDG_CACHE_HOME": str(root / "cache")}

            async def exercise():
                params = StdioServerParameters(command=sys.executable, args=["-m", "capalith.mcp_server"], cwd=PROJECT_ROOT, env=environment)
                async with stdio_client(params) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        response = await session.call_tool("discover", {"query": "anything at all", "workspace": str(root / "work")})
                        self.assertTrue(response.is_error)
                        text = response.content[0].text
                        self.assertTrue(text.startswith("no_skill_sources"), text)
                        self.assertIn(str(custom / "skills"), text)           # the configured host home, not a fixed list
                        self.assertIn(str(root / "work/.claude/skills"), text)
                        self.assertNotIn(str(root / "home/.claude/skills"), text)  # the default Claude home was replaced
                        self.assertIn("SKILL.md", text)
                        self.assertIn("--extra-source /absolute/path", text)
            anyio.run(exercise)

    def test_provision_says_what_it_downloads_how_big_and_where_before_it_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            order: list[str] = []
            notices: list[str] = []

            class Hub:
                @staticmethod
                def snapshot_download(**kwargs):
                    order.append("download")
                    raise RuntimeError("no network in this test")

            with patch.dict(os.environ, {"HOME": str(root), "XDG_CACHE_HOME": str(root / "cache")}, clear=True), \
                    patch.object(semantic.importlib, "import_module", lambda name: Hub):
                with self.assertRaises(semantic.SemanticUnavailable):
                    semantic.provision_model(announce=lambda text: (order.append("announce"), notices.append(text)))
                target = semantic.model_directory()
                megabytes = round(load_settings()["semantic"]["download_bytes"] / 1e6)
            self.assertEqual(["announce", "download"], order)
            text = notices[0]
            self.assertIn(f"{megabytes} MB", text)
            self.assertIn(str(target), text)
            self.assertIn("Hugging Face", text)
            self.assertIn(defaults()["semantic"]["model"]["artifact"], text)
            self.assertIn("once", text)
            self.assertGreater(megabytes, 10)

    def test_the_command_line_prints_the_notice_to_stderr_and_the_result_to_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"HOME": str(root), "XDG_CACHE_HOME": str(root / "cache")}, clear=True):
                def fake(announce=None):
                    announce("NOTICE TEXT")
                    return root / "model"
                with patch.object(semantic, "provision_model", fake), redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(0, semantic.main(["provision"]))
            self.assertEqual("NOTICE TEXT\n", err.getvalue())
            self.assertIn('"path"', out.getvalue())
            self.assertNotIn("NOTICE", out.getvalue())

    def test_both_models_carry_their_download_size(self):
        from capalith.settings import profiles
        self.assertGreater(defaults()["semantic"]["download_bytes"], 10_000_000)
        self.assertGreater(profiles()["arctic-embed-s"]["download_bytes"], 10_000_000)
        self.assertNotIn("download_bytes", defaults()["semantic"]["model"])   # the model's identity (and its index key) is unchanged


if __name__ == "__main__":
    unittest.main()
