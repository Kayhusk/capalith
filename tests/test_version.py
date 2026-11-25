"""`--version` on the three commands: the version and exit 0, from the one number in `capalith.__version__`."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import capalith

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(module: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, *arguments],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
    )


class VersionFlagTests(unittest.TestCase):
    def test_each_command_answers_its_name_and_version_and_exits_zero(self):
        for module, name in (
            ("capalith", "capalith"),
            ("capalith.mcp_server", "capalith-mcp"),
            ("capalith.semantic", "capalith-model"),
        ):
            with self.subTest(command=name):
                result = run(module, "--version")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(f"{name} {capalith.__version__}\n", result.stdout)
                self.assertEqual("", result.stderr)

    def test_the_number_is_the_packages_own_single_source(self):
        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
        self.assertIn('version = { attr = "capalith.__version__" }', pyproject)
        for relative in ("capalith/cli.py", "capalith/mcp_server.py", "capalith/semantic.py"):
            with self.subTest(file=relative):
                text = (PROJECT_ROOT / relative).read_text()
                self.assertNotIn(f'"{capalith.__version__}"', text, "the number is not typed again")

    def test_a_missing_required_argument_still_exits_two(self):
        for module in ("capalith", "capalith.semantic"):
            with self.subTest(command=module):
                self.assertEqual(2, run(module).returncode)
        self.assertEqual(2, run("capalith.mcp_server", "--unknown-flag").returncode)


if __name__ == "__main__":
    unittest.main()
