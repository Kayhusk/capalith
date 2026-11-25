"""Tests make their temporary folders under a path that holds no link.

Capalith refuses a link in the path of its catalog and settings folders (`Store._open_parent`, on purpose). On macOS the
system temporary folder sits under `/var`, a link to `/private/var`, so a test that made a catalog there was refused for a
reason that has nothing to do with what it tests. The temporary folder is resolved once here, before any test makes one.
The tests of the link refusal make their own links inside it and are not affected.
"""
import os
import tempfile

_resolved = os.path.realpath(tempfile.gettempdir())
tempfile.tempdir = _resolved       # the folder this process's tests use
os.environ["TMPDIR"] = _resolved   # and the one a test's child process inherits
