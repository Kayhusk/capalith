"""The lock tool reads every pin or says which line it cannot read, and picks only wheels that fit (offline: releases are injected)."""
from __future__ import annotations

import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import lock_hashes as lock  # noqa: E402

DIGEST = "a" * 64


def wheel(name: str, version: str, tags: str) -> dict:
    return {"filename": f"{name}-{version}-{tags}.whl", "digests": {"sha256": DIGEST}}


class ReadingPinsTests(unittest.TestCase):
    def read(self, text: str) -> list[tuple[str, str]]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.txt"
            path.write_text(text)
            return lock.pins(path)

    def test_every_valid_pinned_form_is_read(self):
        text = (
            "# a comment\n--only-binary=:all:\n\n"
            "alpha==1.0\n"
            "beta == 2.0   # valid pinned requirement\n"
            "gamma==3.1.4 \\\n    --hash=sha256:" + DIGEST + " \\\n    --hash=sha256:" + "b" * 64 + "\n\n"
            "Delta_x.y-z==2026.7.22.post1\n"
        )
        self.assertEqual([("alpha", "1.0"), ("beta", "2.0"), ("gamma", "3.1.4"), ("Delta_x.y-z", "2026.7.22.post1")], self.read(text))

    def test_the_real_requirements_file_gives_every_pin(self):
        path = Path(__file__).resolve().parents[1] / "requirements.txt"
        expected = re.findall(r"^[A-Za-z0-9][\w.-]*==", path.read_text(), re.M)
        self.assertGreater(len(expected), 40)
        self.assertEqual(len(expected), len(lock.pins(path)))

    def test_a_line_it_cannot_read_is_an_error_that_names_the_line_never_skipped(self):
        for line in ("gamma>=1.0", "delta[extra]==1.0", "eps==1.0; python_version < '3.12'", "zeta~=1.2", "eta==1.*",
                     "-r other.txt", "--index-url https://example.invalid/simple", "git+https://example.invalid/x.git#egg=x",
                     "theta", "iota==", "kappa==1.0 --global-option=x", "--hash=sha256:" + DIGEST):
            with self.subTest(line):
                with self.assertRaises(lock.LockError) as caught:
                    self.read(f"alpha==1.0\n\n{line}\n")
                self.assertIn("line 3", str(caught.exception))
                self.assertIn(line.split()[0], str(caught.exception))

    def test_a_duplicate_pin_and_an_empty_file_are_errors(self):
        with self.assertRaises(lock.LockError):
            self.read("alpha==1.0\nAlpha == 2.0\n")
        with self.assertRaises(lock.LockError):
            self.read("# nothing here\n")


class FittingWheelTests(unittest.TestCase):
    def test_python_versions(self):
        cases = [
            ("py2", "none", 11, False),            # a Python 2 only wheel is not a Python 3 wheel
            ("py2.py3", "none", 11, True),
            ("py3", "none", 11, True),
            ("py311", "none", 11, True),
            ("py310", "none", 11, True),           # py3N means 3.N or later
            ("py313", "none", 11, False),
            ("py30", "none", 11, True),
            ("cp311", "cp311", 11, True),
            ("cp312", "cp312", 11, False),
            ("cp39", "abi3", 11, True),
            ("cp312", "abi3", 11, False),
            ("cp311.cp312", "cp311.cp312", 11, True),    # compressed tag sets: any listed pair that fits
            ("cp312.cp313", "cp312.cp313", 11, False),
        ]
        for py, abi, minor, expected in cases:
            with self.subTest(py=py, abi=abi, minor=minor):
                self.assertEqual(expected, lock.python_fits(py, abi, minor))

    def test_platforms(self):
        self.assertTrue(lock.platform_fits("manylinux_2_28_x86_64.manylinux2014_x86_64", "linux-x86_64"))
        self.assertFalse(lock.platform_fits("manylinux_2_40_x86_64", "linux-x86_64"))
        self.assertFalse(lock.platform_fits("manylinux_2_28_aarch64", "linux-x86_64"))
        self.assertTrue(lock.platform_fits("macosx_11_0_arm64", "macos-arm64"))
        self.assertFalse(lock.platform_fits("macosx_16_0_arm64", "macos-arm64"))
        self.assertTrue(lock.platform_fits("any", "macos-arm64"))


class MainTests(unittest.TestCase):
    def run_main(self, requirements: str, releases: dict, *arguments: str):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requirements.txt").write_text(requirements)
            out, err = io.StringIO(), io.StringIO()

            def release(name, version):
                if releases is None:
                    raise AssertionError("PyPI must not be asked when a requirement cannot be read")
                return releases[(name, version)]

            with patch.object(lock, "ROOT", root), patch.object(lock, "release_files", release), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = lock.main(list(arguments))
            written = sorted(p.name for p in root.glob("requirements-*.txt"))
            texts = {p.name: p.read_text() for p in root.glob("requirements-*.txt")}
            return code, out.getvalue() + err.getvalue(), written, texts

    def test_an_unreadable_requirement_stops_both_modes_before_anything_is_asked_or_written(self):
        text = "alpha==1.0\nbeta == 2.0 # fine\ngamma>=1.0\n"
        for arguments in ((), ("--check",)):
            with self.subTest(arguments=arguments):
                code, output, written, _ = self.run_main(text, None, *arguments)
                self.assertEqual(2, code)
                self.assertIn("line 3", output)
                self.assertEqual([], written)

    def test_spaced_and_commented_pins_are_both_locked(self):
        releases = {("alpha", "1.0"): [wheel("alpha", "1.0", "py3-none-any")], ("beta", "2.0"): [wheel("beta", "2.0", "py2.py3-none-any")]}
        code, output, written, texts = self.run_main("alpha==1.0\nbeta == 2.0 # valid pinned requirement\n", releases)
        self.assertEqual(0, code)
        self.assertIn("2 of 2 pins", output)
        self.assertEqual(4, len(written))
        for text in texts.values():
            self.assertIn(f"alpha==1.0 \\\n    --hash=sha256:{DIGEST}", text)
            self.assertIn(f"beta==2.0 \\\n    --hash=sha256:{DIGEST}", text)

    def test_check_writes_nothing_and_a_python_2_only_wheel_is_missing_not_a_success(self):
        releases = {("alpha", "1.0"): [wheel("alpha", "1.0", "py3-none-any")], ("beta", "2.0"): [wheel("beta", "2.0", "py2-none-any")]}
        code, output, written, _ = self.run_main("alpha==1.0\nbeta==2.0\n", releases, "--check")
        self.assertEqual(1, code)
        self.assertIn("beta==2.0", output)
        self.assertIn("1 of 2 pins", output)
        self.assertEqual([], written)


if __name__ == "__main__":
    unittest.main()
