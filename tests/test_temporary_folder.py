"""The temporary folder the tests use has no link in its path (see tests/__init__.py)."""
import os
import tempfile
import unittest
from pathlib import Path


class TemporaryFolderTests(unittest.TestCase):
    def test_a_test_folder_is_its_own_real_path(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(os.path.realpath(directory), directory)
            self.assertEqual(Path(directory).resolve(), Path(directory))

    def test_a_child_process_inherits_the_resolved_folder(self):
        self.assertEqual(os.path.realpath(os.environ["TMPDIR"]), os.environ["TMPDIR"])
        self.assertEqual(tempfile.gettempdir(), os.environ["TMPDIR"])


if __name__ == "__main__":
    unittest.main()
