"""A server whose data folder sits behind a link says a link is the cause. The refusal itself does not change.

The catalog's folder is opened one part at a time without following a link (`Store._open_parent`), on purpose. A user whose
data folder, or a folder above it, is a link used to get `catalog_setup_failed` with the cause hidden. Now the error names
the link and what to do; any other setup failure keeps its old text. Links are made on Linux; on macOS the same rule
refuses `/var`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
from mcp import StdioServerParameters

from tests.helpers import make_skill
from tests.test_newer_catalog import call

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADVICE = "Capalith does not follow links there. Point XDG_DATA_HOME or storage.data_dir at the real folder."


def start(skills: Path, data_home: Path | str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "capalith.mcp_server", "--source", str(skills)],
        input=b"", capture_output=True, cwd=PROJECT_ROOT, timeout=60,
        env={**os.environ, "XDG_DATA_HOME": str(data_home)},
    )


class LinkErrorTests(unittest.TestCase):
    def make(self, temp: Path) -> tuple[Path, Path]:
        skills = temp / "skills"
        make_skill(skills, "demo-skill", b"demo")
        real = temp / "real"
        real.mkdir()
        return skills, real

    def test_a_data_folder_that_is_a_link_says_so_and_what_to_do(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory).resolve()
            skills, real = self.make(temp)
            (temp / "linked-data").symlink_to(real)
            result = start(skills, temp / "linked-data")
            self.assertEqual(1, result.returncode)
            self.assertEqual(
                f"catalog_link_refused: a link in the path of the catalog folder is refused: {temp / 'linked-data'}. {ADVICE}\n".encode(),
                result.stderr)
            self.assertEqual([], list(real.iterdir()), "the refusal stands: nothing is written behind the link")

    def test_a_link_above_the_data_folder_says_which_part_is_the_link(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory).resolve()
            skills, real = self.make(temp)
            (temp / "home-link").symlink_to(real)
            result = start(skills, temp / "home-link/nested/data")
            self.assertEqual(1, result.returncode)
            self.assertEqual(
                f"catalog_link_refused: a link in the path of the catalog folder is refused: {temp / 'home-link'}. {ADVICE}\n".encode(),
                result.stderr)
            self.assertEqual([], list(real.iterdir()))

    def test_a_setup_failure_that_is_not_a_link_keeps_its_old_text(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory).resolve()
            skills, _ = self.make(temp)
            (temp / "a-file").write_text("not a folder")
            result = start(skills, temp / "a-file")
            self.assertEqual(1, result.returncode)
            self.assertTrue(result.stderr.startswith(b"catalog_setup_failed:"), result.stderr)
            self.assertNotIn(b"catalog_link_refused", result.stderr)

    def test_the_link_is_shown_escaped_on_one_line(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory).resolve()
            skills, real = self.make(temp)
            (temp / "bad\nFORGED‮").symlink_to(real)
            result = start(skills, temp / "bad\nFORGED‮/data")
            self.assertEqual(1, result.returncode)
            self.assertEqual(1, len(result.stderr.splitlines()), result.stderr)
            self.assertIn(b"bad\\x0aFORGED\\u202e", result.stderr)

    def test_an_agent_managed_server_answers_with_the_same_sentence(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory).resolve()
            skills, real = self.make(temp)
            (temp / "linked-data").symlink_to(real)
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "capalith.mcp_server"], cwd=PROJECT_ROOT,
                env={"XDG_DATA_HOME": str(temp / "linked-data")})
            is_error, text = anyio.run(call, parameters, "discover", {"query": "demo", "source_paths": [str(skills)]})
            self.assertTrue(is_error)
            self.assertEqual(
                f"catalog_link_refused: a link in the path of the catalog folder is refused: {temp / 'linked-data'}. {ADVICE}", text)
            self.assertEqual([], list(real.iterdir()))


if __name__ == "__main__":
    unittest.main()
