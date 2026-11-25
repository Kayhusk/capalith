"""The macOS way of opening the catalog (no /proc): the folder's path from F_GETPATH, then the same checks.

The macOS branch runs on Linux through two seams: `store._is_macos` (the platform test) and `store._folder_path` (the
F_GETPATH call), here answered from /proc, which only the test uses. On a Mac the same tests run against the product's own
F_GETPATH route (`folder_path` below), so the swap and replace checks are exercised there too, not skipped.
Only `test_linux_keeps_the_proc_path_byte_for_byte` is Linux-only: macOS has no /proc/self/fd route to keep.
"""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from capalith import store as store_module
from capalith.store import Store, StoreError


def proc_folder_path(descriptor: int) -> str:
    return os.readlink(f"/proc/self/fd/{descriptor}")


# The call that tells the folder's path from an open descriptor: the real F_GETPATH route on a Mac, /proc elsewhere.
folder_path = store_module._macos_folder_path if sys.platform == "darwin" else proc_folder_path


class Recorder:
    """Records the first argument of every sqlite3.connect the store makes."""

    def __init__(self):
        self.targets: list[str] = []
        self._real = sqlite3.connect

    def __call__(self, target, *args, **kwargs):
        self.targets.append(str(target))
        return self._real(target, *args, **kwargs)


class MacOSAnchorTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only: macOS has no /proc/self/fd route; it opens by the folder's path")
    def test_linux_keeps_the_proc_path_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            recorder = Recorder()
            with patch.object(store_module.sqlite3, "connect", recorder):
                store = Store(root / "catalog.sqlite3")
                store.initialize()
            self.assertTrue(recorder.targets)
            for target in recorder.targets:
                self.assertRegex(target, r"^/proc/self/fd/\d+/catalog\.sqlite3$")

    def test_macos_opens_by_the_folders_path_and_the_catalog_works(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            recorder = Recorder()
            with patch.object(store_module, "_is_macos", lambda: True), \
                    patch.object(store_module, "_folder_path", folder_path), \
                    patch.object(store_module.sqlite3, "connect", recorder):
                store = Store(root / "catalog.sqlite3")
                store.initialize()
                self.assertEqual([], store.list_sources())
                read = Store(root / "catalog.sqlite3", read_only=True)
                self.assertEqual([], read.list_sources())
            self.assertTrue(recorder.targets)
            self.assertNotIn("/proc/", "".join(recorder.targets))
            self.assertTrue(all(str(root / "catalog.sqlite3") in target for target in recorder.targets), recorder.targets)

    def test_a_folder_swapped_between_the_path_and_the_open_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            folder = root / "data"
            folder.mkdir()
            first = Store(folder / "catalog.sqlite3")
            first.initialize()

            def swap_after_the_path_is_read(descriptor: int) -> str:
                path = folder_path(descriptor)
                if not swapped:
                    swapped.append(True)
                    folder.rename(root / "data-moved")
                    folder.mkdir()
                    other = Store(folder / "catalog.sqlite3")  # an attacker's folder with a catalog of its own
                    other.initialize()
                return path

            swapped: list[bool] = []
            with patch.object(store_module, "_is_macos", lambda: True), \
                    patch.object(store_module, "_folder_path", swap_after_the_path_is_read):
                with self.assertRaisesRegex(StoreError, "database folder changed while being opened"):
                    Store(folder / "catalog.sqlite3")._connect().close()
            self.assertTrue(swapped)

    def test_a_file_replaced_between_the_path_and_the_open_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = Store(root / "catalog.sqlite3")
            first.initialize()
            swapped: list[bool] = []

            def replace_the_file(descriptor: int) -> str:
                path = folder_path(descriptor)
                if not swapped:
                    swapped.append(True)
                    (root / "catalog.sqlite3").rename(root / "kept.sqlite3")
                    Store(root / "catalog.sqlite3").initialize()
                return path

            with patch.object(store_module, "_is_macos", lambda: True), \
                    patch.object(store_module, "_folder_path", replace_the_file):
                with self.assertRaises(StoreError):
                    Store(root / "catalog.sqlite3")._connect().close()

    def test_known_limit_a_swap_restored_before_the_checks_is_not_caught(self):
        """KNOWN LIMIT of the path route, not protection: this asserts what happens today so the limit stays written down.

        The folder is swapped for another during SQLite's open and swapped back before the checks that follow. Every
        check passes (the path leads to the held folder again, the held file is at its name again) and the connection
        reads the other database. Linux's held-folder route cannot be fooled this way; a persistent swap is caught
        (tests above), a restored one is not, and neither is a swap after the checks, or of the journal files.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            folder, other = root / "data", root / "other"
            for place, mark in ((folder, "A"), (other, "B")):
                place.mkdir()
                Store(place / "catalog.sqlite3").initialize()
                database = sqlite3.connect(place / "catalog.sqlite3")
                database.execute("CREATE TABLE marker(v TEXT)")
                database.execute("INSERT INTO marker VALUES (?)", (mark,))
                database.commit()
                database.close()
            real = sqlite3.connect

            def connect_while_swapped(target, *args, **kwargs):
                folder.rename(root / "held")
                other.rename(folder)                       # the other folder is at the path while SQLite opens
                connection = real(target, *args, **kwargs)
                folder.rename(other)
                (root / "held").rename(folder)             # restored before the checks
                return connection

            with patch.object(store_module, "_is_macos", lambda: True), \
                    patch.object(store_module, "_folder_path", folder_path), \
                    patch.object(store_module.sqlite3, "connect", connect_while_swapped):
                connection = Store(folder / "catalog.sqlite3")._connect()      # no StoreError: the limit
            try:
                self.assertEqual("B", connection.execute("SELECT v FROM marker").fetchone()[0])
            finally:
                connection.close()

    @unittest.skipUnless(sys.platform == "darwin", "macOS only: F_GETPATH exists only there; elsewhere the fake call below covers the reading")
    def test_the_real_f_getpath_names_the_held_folder_and_is_the_route_the_store_uses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.assertEqual(os.path.realpath(root), store_module._macos_folder_path(descriptor))
            finally:
                os.close(descriptor)
            self.assertIs(store_module._macos_folder_path, store_module._folder_path)
            recorder = Recorder()
            with patch.object(store_module.sqlite3, "connect", recorder):
                Store(root / "catalog.sqlite3").initialize()
            self.assertTrue(recorder.targets)
            self.assertNotIn("/proc/", "".join(recorder.targets))
            self.assertTrue(all(str(root / "catalog.sqlite3") in target for target in recorder.targets), recorder.targets)

    def test_the_descriptor_listing_has_a_dev_fd_route_for_a_system_without_proc(self):
        from tests.helpers import open_descriptors
        real_isdir = os.path.isdir
        with patch("os.path.isdir", lambda path: False if path == "/proc/self/fd" else real_isdir(path)):
            held = os.open(os.devnull, os.O_RDONLY)
            try:
                self.assertIn(str(held), open_descriptors())    # listed through /dev/fd, as on a Mac
            finally:
                os.close(held)

    def test_the_real_f_getpath_call_is_read_as_a_nul_padded_path(self):
        seen = {}

        def fake_fcntl(descriptor, command, argument):
            seen.update(descriptor=descriptor, command=command, size=len(argument))
            return b"/Users/someone/caf\xc3\xa9/data\0" + bytes(1000)

        stub = types.SimpleNamespace(F_GETPATH=50, fcntl=fake_fcntl)
        with patch.dict(sys.modules, {"fcntl": stub}):
            self.assertEqual("/Users/someone/café/data", store_module._macos_folder_path(7))
        self.assertEqual({"descriptor": 7, "command": 50, "size": 1024}, seen)


if __name__ == "__main__":
    unittest.main()
