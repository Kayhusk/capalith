"""Paths in the new error texts (review of 705765a): control characters shown as code points, a chmod that survives a shell,
and none offered for a name that holds control characters. The checks themselves are not loosened and no legitimate path is refused.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

from capalith import answer
from capalith.settings import SettingsError, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORDINARY = {"space": "a b", "quote": "it's here", "semicolon": "x;touch pwned", "dollar": "$(true)"}
CONTROL = {"newline": "line\nbreak: injected", "escape": "esc\x1b[31mred", "bidi": "rtl‮over"}


def writable_settings(root: Path, folder_name: str) -> Path:
    folder = root / folder_name
    folder.mkdir(parents=True)
    os.chmod(folder, 0o700)
    path = folder / "settings.toml"
    path.write_text("version = 1\n")
    os.chmod(path, 0o664)     # writable by the group: refused, and the one fix is chmod go-w
    return path


class SettingsErrorTextTests(unittest.TestCase):
    def message(self, path: Path) -> str:
        with patch.dict(os.environ, {"HOME": str(path.parent), "CAPALITH_CONFIG": str(path)}, clear=True):
            with self.assertRaises(SettingsError) as raised:
                load_settings()
        return str(raised.exception)

    def test_an_ordinary_path_gets_a_chmod_that_a_shell_reads_as_one_command_on_that_path(self):
        for label, name in ORDINARY.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                path = writable_settings(Path(directory), name)
                message = self.message(path)
                self.assertEqual(1, len(message.splitlines()), message)
                advice = message.split("Fix: ", 1)[1]
                self.assertEqual(["chmod", "go-w", str(path)], shlex.split(advice), advice)

    def test_a_path_with_control_characters_is_shown_as_code_points_and_offered_no_command(self):
        for label, name in CONTROL.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                path = writable_settings(Path(directory), name)
                message = self.message(path)
                self.assertEqual(1, len(message.splitlines()), repr(message))
                self.assertTrue(all(ord(c) >= 32 and c != "\x7f" and not 0x202a <= ord(c) <= 0x202e for c in message), repr(message))
                self.assertNotIn("chmod", message)
                self.assertIn("control character", message)
                self.assertEqual(answer._clean(str(path)), message.split(": ", 2)[2].split(" can be written")[0].split(" belongs")[0].split(" is not")[0].split(" has more")[0])

    def test_through_the_command_line_the_message_is_one_clean_line(self):
        for label, name in {**ORDINARY, **CONTROL}.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = writable_settings(root, name)
                done = subprocess.run([sys.executable, "-m", "capalith", "--db", str(root / "c.sqlite3"), "--config", str(path), "discover", "q"],
                                      cwd=PROJECT_ROOT, env={"PATH": os.environ["PATH"], "HOME": str(root)}, capture_output=True, text=True)
                self.assertEqual(2, done.returncode, done.stderr)
                lines = done.stderr.strip().splitlines()
                self.assertEqual(1, len(lines), repr(done.stderr))
                self.assertNotIn("\x1b", done.stderr)
                self.assertTrue(lines[0].startswith("settings_untrusted: file: "), lines[0])

    def test_through_the_server_the_message_is_one_clean_line(self):
        for label, name in {**ORDINARY, **CONTROL}.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = writable_settings(root, name)
                environment = {"PATH": os.environ["PATH"], "HOME": str(root), "CAPALITH_CONFIG": str(path)}

                async def exercise():
                    params = StdioServerParameters(command=sys.executable, args=["-m", "capalith.mcp_server"], cwd=PROJECT_ROOT, env=environment)
                    async with stdio_client(params) as streams:
                        async with ClientSession(*streams) as session:
                            await session.initialize()
                            response = await session.call_tool("discover", {"query": "anything", "workspace": str(root)})
                            self.assertTrue(response.is_error)
                            text = response.content[0].text
                            self.assertEqual(1, len(text.splitlines()), repr(text))
                            self.assertNotIn("\x1b", text)
                            self.assertTrue(text.startswith("settings_untrusted"), text)
                anyio.run(exercise)


class NoSkillSourcesTextTests(unittest.TestCase):
    def test_a_configured_folder_with_any_name_is_listed_safely(self):
        for label, name in {**ORDINARY, **CONTROL}.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                for sub in ("home", "config/capalith", "data", "cache", "work"):
                    (root / sub).mkdir(parents=True)
                os.chmod(root / "config", 0o700)
                os.chmod(root / "config/capalith", 0o700)
                odd = root / name
                settings = root / "config/capalith/settings.toml"
                settings.write_text("version = 1\n[sources.hosts.claude]\nhome_default = " + json.dumps(str(odd)) + "\n")
                settings.chmod(0o600)
                environment = {"PATH": os.environ["PATH"], "HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "config"),
                               "XDG_DATA_HOME": str(root / "data"), "XDG_CACHE_HOME": str(root / "cache")}

                async def exercise():
                    params = StdioServerParameters(command=sys.executable, args=["-m", "capalith.mcp_server"], cwd=PROJECT_ROOT, env=environment)
                    async with stdio_client(params) as streams:
                        async with ClientSession(*streams) as session:
                            await session.initialize()
                            response = await session.call_tool("discover", {"query": "anything", "workspace": str(root / "work")})
                            self.assertTrue(response.is_error)
                            text = response.content[0].text
                            self.assertEqual(1, len(text.splitlines()), repr(text))
                            self.assertTrue(all(ord(c) >= 32 and c != "\x7f" and not 0x202a <= ord(c) <= 0x202e for c in text), repr(text))
                            self.assertIn(answer._clean(str(odd / "skills")), text)
                            self.assertTrue(text.startswith("no_skill_sources"), text)
                anyio.run(exercise)


class JudgeOrderTests(unittest.TestCase):
    def test_an_accepted_ai_result_wins_over_the_local_gate_and_an_empty_one_is_no_match(self):
        from tests.test_answer import CONFIG, candidate, result
        refusing = {**CONFIG, "refuse_below": 0.7}
        ranked = result([candidate("pdf-filler", similarity=None)])
        ranked["ranking"] = {"path": "ai"}
        ranked["semantic"]["best_similarity"] = 0.2            # the combined signal: the AI chose it, the local score is low
        self.assertEqual("good", answer.judge(ranked, refusing)[0])
        self.assertEqual("good", answer.judge(ranked, CONFIG)[0])        # gate off: the same
        empty = {**result([]), "ranking": {"path": "ai", "abstained": True}}
        empty["semantic"]["best_similarity"] = 0.9
        self.assertEqual("no_match", answer.judge(empty, refusing)[0])
        # the local gate still applies to free results, and exact still comes before the AI path
        free = result([candidate("pdf-filler", similarity=0.9)])
        free["semantic"]["best_similarity"] = 0.2
        self.assertEqual("no_match", answer.judge(free, refusing)[0])
        exact = result([candidate("pdf-filler")], query="pdf filler")
        exact["ranking"] = {"path": "ai"}
        self.assertEqual("exact", answer.judge(exact, refusing)[0])


class DescriptionTests(unittest.TestCase):
    def test_the_description_says_how_many_by_default_and_promises_no_recognition_of_tasks_without_a_skill(self):
        from capalith import mcp_server
        text = mcp_server._WHEN_TO_CALL
        self.assertIn("up to 3 skills by default", text)
        self.assertNotIn("says plainly that nothing fits", text)
        self.assertNotIn("nothing fits", text)
        self.assertIn("candidate", text)


if __name__ == "__main__":
    unittest.main()
