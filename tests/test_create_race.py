"""Two processes creating the same new file at the same moment: macOS can answer one of them ENOENT.

On macOS, `openat(directory descriptor, name, O_CREAT)` without O_EXCL can fail with ENOENT for the process that lost a race to
create the same new name, although the file exists by then (reported by others too: golang/go#81246 and its reproduction). The
same call with O_EXCL answers the loser EEXIST, correctly. Capalith's creating opens are therefore written as: create with
O_EXCL; if the name exists, open it without O_CREAT; if it vanished in between, look again, a few times. The tests make a
`MacOpen` stand in for that kernel behavior (once per name, the file is created and the plain create says ENOENT) and run the
real code through it: a deterministic stand-in, not a measurement of macOS (the macOS CI diagnostic measured that).
"""
from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capalith.onboarding import prepare_catalog
from capalith.store import Store
from tests.helpers import make_skill


class MacOpen:
    """`os.open` as macOS does when two processes create one name together: the plain create through a directory descriptor
    loses once per name, with ENOENT, after the other creator made the file."""

    def __init__(self):
        self.real = os.open
        self.lost: list[str] = []       # names whose plain create it failed
        self.creates: list[str] = []    # every name opened with O_CREAT through a directory descriptor, plain or exclusive

    def __call__(self, path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and flags & os.O_CREAT:
            self.creates.append(path)
        if dir_fd is not None and flags & os.O_CREAT and not flags & os.O_EXCL and path not in self.lost:
            try:
                os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                self.lost.append(path)
                self.real(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)   # the other creator's file
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        return self.real(path, flags, mode, dir_fd=dir_fd)


class CreatingOpensTests(unittest.TestCase):
    def test_the_lock_file_of_the_first_start_survives_losing_the_create_race(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            make_skill(root / "skills", "demo-skill", b"demo")
            standin = MacOpen()
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data")}), patch("os.open", standin):
                database = prepare_catalog([root / "skills"])
            self.assertTrue(any(name.endswith(".lock") for name in standin.creates), "the lock's create went through the stand-in")
            self.assertTrue(database.exists())

    def test_the_catalog_file_survives_losing_the_create_race(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            standin = MacOpen()
            with patch("os.open", standin):
                Store(root / "catalog.sqlite3").initialize()
            self.assertIn("catalog.sqlite3", standin.creates)
            self.assertEqual([], Store(root / "catalog.sqlite3", read_only=True).list_sources())

    def test_the_usage_log_files_survive_losing_the_create_race(self):
        import time
        from capalith import usage
        from tests.test_usage_log import logging_to, result_with
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory).resolve() / "usage"
            folder.mkdir(mode=0o700)
            standin = MacOpen()
            with logging_to(folder), patch("os.open", standin):
                before = usage.failures()
                usage.record_search("discover", time.perf_counter(), result_with("not_configured"))
                self.assertEqual(before, usage.failures(), "no line dropped")
            self.assertTrue({"usage.lock", "usage.jsonl"} <= set(standin.creates))
            self.assertEqual(1, len((folder / "usage.jsonl").read_text().splitlines()))


class OpenOrCreateTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name).resolve()
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW

    def open(self, name="x.lock"):
        from capalith.store import open_or_create
        return open_or_create(name, self.flags, 0o600, dir_fd=self.fd)

    def test_it_creates_a_new_file_private_to_the_owner_and_opens_an_existing_one(self):
        descriptor = self.open()
        os.write(descriptor, b"kept")
        os.close(descriptor)
        self.assertEqual(0o600, (self.root / "x.lock").stat().st_mode & 0o777)
        again = self.open()
        self.assertEqual(b"kept", os.pread(again, 10, 0))
        os.close(again)

    def test_it_gets_a_descriptor_when_the_plain_create_loses_the_race(self):
        standin = MacOpen()
        with patch("os.open", standin):
            descriptor = self.open()
        os.close(descriptor)
        self.assertTrue((self.root / "x.lock").exists())

    def test_a_file_removed_between_the_exists_answer_and_the_open_is_created_again(self):
        real = os.open
        calls = []

        def vanishing(path, flags, mode=0o777, *, dir_fd=None):
            calls.append(flags & os.O_EXCL != 0)
            if len(calls) == 2:                              # the plain open after EEXIST: the file was just removed
                (self.root / "x.lock").unlink()
            return real(path, flags, mode, dir_fd=dir_fd)

        (self.root / "x.lock").write_text("old")
        with patch("os.open", vanishing):
            descriptor = self.open()
        os.close(descriptor)
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue((self.root / "x.lock").exists())

    def test_a_real_missing_folder_is_still_an_error_after_a_few_looks(self):
        def gone(path, flags, mode=0o777, *, dir_fd=None):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)

        with patch("os.open", gone), self.assertRaises(FileNotFoundError):
            self.open()

    def test_a_link_in_the_place_of_the_file_is_still_refused_and_not_followed(self):
        target = self.root / "elsewhere"
        target.write_text("not ours")
        (self.root / "x.lock").symlink_to(target)
        with self.assertRaises(OSError):
            self.open()
        self.assertEqual("not ours", target.read_text())


if __name__ == "__main__":
    unittest.main()
