from __future__ import annotations

import hashlib
import os
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ARTIFACT_ID_DOMAIN = b"capalith-artifact-v1\0"
BUNDLE_DIGEST_DOMAIN = b"capalith-bundle-v1\0"
_CHUNK_SIZE = 1024 * 1024
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_Metadata = tuple[int, int, int, int, int, int, int]
_DirectoryRecord = tuple[str, bytes, int, _Metadata]
_FileEntry = tuple[str, bytes, int, str, _Metadata]


class BundleEntryError(Exception):
    """Raised when a skill entry is unsupported or changes while being read."""


@dataclass(frozen=True)
class FileRecord:
    path: str
    executable: bool
    size: int
    sha256: str


@dataclass(frozen=True)
class BundleManifest:
    digest: str
    files: tuple[FileRecord, ...]
    byte_count: int


def artifact_id(source_id: str, bundle_path: str) -> str:
    normalized_path = posixpath.normpath(bundle_path)
    digest = hashlib.sha256(
        ARTIFACT_ID_DOMAIN
        + source_id.encode("utf-8")
        + b"\0"
        + normalized_path.encode("utf-8")
    ).hexdigest()
    return f"art_{digest}"


def _entry_error(path: Path, reason: str) -> BundleEntryError:
    return BundleEntryError(f"cannot use {path!s}: {reason}")


def _metadata(file_stat: os.stat_result) -> _Metadata:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_mode & 0o111,
    )


def _require_regular(path: Path, file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise _entry_error(path, "entry is not a regular file")


def _require_directory(path: Path, file_stat: os.stat_result) -> None:
    if not stat.S_ISDIR(file_stat.st_mode):
        raise _entry_error(path, "entry is not a directory")


def _list_directory(
    path: Path, descriptor: int
) -> list[tuple[str, bytes, os.stat_result]]:
    entries: list[tuple[str, bytes, os.stat_result]] = []
    for name in os.listdir(descriptor):
        try:
            encoded_name = name.encode("utf-8")
        except UnicodeEncodeError as error:
            raise _entry_error(path / name, "path is not valid UTF-8") from error
        entry_stat = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        entries.append((name, encoded_name, entry_stat))
    entries.sort(key=lambda item: item[1])
    return entries


def _collect_files(
    bundle_root: Path,
    directory_descriptors: list[int],
    directories: list[_DirectoryRecord],
    snapshots: dict[int, tuple[tuple[bytes, _Metadata], ...]],
) -> list[_FileEntry]:
    files: list[_FileEntry] = []
    directory_index = 0

    # Empty directories do not affect bundle identity.
    while directory_index < len(directories):
        relative, encoded_relative, descriptor, _directory_metadata = directories[
            directory_index
        ]
        directory_path = bundle_root / relative
        entries = _list_directory(directory_path, descriptor)
        snapshots[descriptor] = tuple(
            (encoded_name, _metadata(entry_stat))
            for _name, encoded_name, entry_stat in entries
        )

        for name, encoded_name, entry_stat in entries:
            entry_relative = f"{relative}/{name}" if relative else name
            encoded_entry_relative = (
                encoded_relative + b"/" + encoded_name
                if encoded_relative
                else encoded_name
            )
            entry_path = bundle_root / entry_relative

            if stat.S_ISLNK(entry_stat.st_mode):
                raise _entry_error(entry_path, "symbolic links are not supported")
            if stat.S_ISDIR(entry_stat.st_mode):
                child_descriptor = os.open(
                    name, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor
                )
                directory_descriptors.append(child_descriptor)
                child_stat = os.fstat(child_descriptor)
                _require_directory(entry_path, child_stat)
                if _metadata(entry_stat) != _metadata(child_stat):
                    raise _entry_error(entry_path, "entry changed while being inspected")
                directories.append(
                    (
                        entry_relative,
                        encoded_entry_relative,
                        child_descriptor,
                        _metadata(entry_stat),
                    )
                )
            elif stat.S_ISREG(entry_stat.st_mode):
                files.append(
                    (
                        entry_relative,
                        encoded_entry_relative,
                        descriptor,
                        name,
                        _metadata(entry_stat),
                    )
                )
            else:
                raise _entry_error(
                    entry_path, "entry is not a regular file or directory"
                )

        directory_index += 1

    files.sort(key=lambda item: item[1])
    return files


def _hash_file(
    path: Path,
    encoded_relative: bytes,
    parent_descriptor: int,
    name: str,
    initial_metadata: _Metadata,
    bundle_hash: Any,
) -> FileRecord:
    descriptor = os.open(name, _FILE_OPEN_FLAGS, dir_fd=parent_descriptor)
    try:
        before = os.fstat(descriptor)
        _require_regular(path, before)
        if initial_metadata != _metadata(before):
            raise _entry_error(path, "entry changed while being hashed")

        executable = bool(before.st_mode & 0o111)
        size = before.st_size
        bundle_hash.update(b"F")
        bundle_hash.update(len(encoded_relative).to_bytes(8, "big"))
        bundle_hash.update(encoded_relative)
        bundle_hash.update(bytes((executable,)))
        bundle_hash.update(size.to_bytes(8, "big"))

        file_hash = hashlib.sha256()
        bytes_read = 0
        while chunk := os.read(descriptor, _CHUNK_SIZE):
            bytes_read += len(chunk)
            file_hash.update(chunk)
            bundle_hash.update(chunk)

        after = os.fstat(descriptor)
        _require_regular(path, after)
        if initial_metadata != _metadata(after) or bytes_read != size:
            raise _entry_error(path, "entry changed while being hashed")
    finally:
        os.close(descriptor)

    final = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    _require_regular(path, final)
    if initial_metadata != _metadata(final):
        raise _entry_error(path, "entry changed while being hashed")

    return FileRecord(
        path=encoded_relative.decode("utf-8"),
        executable=executable,
        size=size,
        sha256=f"sha256:{file_hash.hexdigest()}",
    )


def _verify_directories(
    bundle_root: Path,
    directories: list[_DirectoryRecord],
    snapshots: dict[int, tuple[tuple[bytes, _Metadata], ...]],
) -> None:
    for relative, _encoded_relative, descriptor, initial_metadata in directories:
        directory_path = bundle_root / relative
        entries = _list_directory(directory_path, descriptor)
        final_snapshot = tuple(
            (encoded_name, _metadata(entry_stat))
            for _name, encoded_name, entry_stat in entries
        )
        if snapshots[descriptor] != final_snapshot:
            raise _entry_error(directory_path, "directory changed while being hashed")

        final_stat = os.fstat(descriptor)
        _require_directory(directory_path, final_stat)
        if initial_metadata != _metadata(final_stat):
            raise _entry_error(directory_path, "directory changed while being hashed")


def _hash_bundle_descriptor(
    bundle_root: Path,
    root_descriptor: int,
    root_metadata: _Metadata,
) -> BundleManifest:
    directory_descriptors = [root_descriptor]
    try:
        try:
            current_root = os.fstat(root_descriptor)
            _require_directory(bundle_root, current_root)
            if root_metadata != _metadata(current_root):
                raise _entry_error(bundle_root, "skill directory changed while being read")

            directories: list[_DirectoryRecord] = [
                ("", b"", root_descriptor, root_metadata)
            ]
            snapshots: dict[int, tuple[tuple[bytes, _Metadata], ...]] = {}
            files = _collect_files(
                bundle_root, directory_descriptors, directories, snapshots
            )

            if not any(relative == "SKILL.md" for relative, *_rest in files):
                raise _entry_error(
                    bundle_root / "SKILL.md", "entry is not a regular file"
                )

            bundle_hash = hashlib.sha256(BUNDLE_DIGEST_DOMAIN)
            records = tuple(
                _hash_file(
                    bundle_root / relative,
                    encoded_relative,
                    parent_descriptor,
                    name,
                    initial_metadata,
                    bundle_hash,
                )
                for (
                    relative,
                    encoded_relative,
                    parent_descriptor,
                    name,
                    initial_metadata,
                ) in files
            )

            _verify_directories(bundle_root, directories, snapshots)
            final_root_path_stat = os.stat(bundle_root, follow_symlinks=False)
            _require_directory(bundle_root, final_root_path_stat)
            if root_metadata != _metadata(final_root_path_stat):
                raise _entry_error(bundle_root, "skill directory changed while being read")
            return BundleManifest(
                digest=f"sha256:{bundle_hash.hexdigest()}",
                files=records,
                byte_count=sum(record.size for record in records),
            )
        except BundleEntryError:
            raise
        except OSError as error:
            raise BundleEntryError(f"could not read skill files: {error}") from error
    finally:
        for descriptor in reversed(directory_descriptors[1:]):
            try:
                os.close(descriptor)
            except OSError:
                pass


def hash_bundle(bundle_root: Path) -> BundleManifest:
    root_descriptor: int | None = None
    try:
        try:
            os.fspath(bundle_root).encode("utf-8")
        except UnicodeEncodeError as error:
            raise _entry_error(bundle_root, "path is not valid UTF-8") from error

        root_descriptor = os.open(bundle_root, _DIRECTORY_OPEN_FLAGS)
        root_stat = os.fstat(root_descriptor)
        _require_directory(bundle_root, root_stat)
        root_metadata = _metadata(root_stat)

        root_path_stat = os.stat(bundle_root, follow_symlinks=False)
        _require_directory(bundle_root, root_path_stat)
        if root_metadata != _metadata(root_path_stat):
            raise _entry_error(bundle_root, "skill directory changed while being read")

        return _hash_bundle_descriptor(bundle_root, root_descriptor, root_metadata)
    except BundleEntryError:
        raise
    except OSError as error:
        raise BundleEntryError(f"could not read skill files: {error}") from error
    finally:
        if root_descriptor is not None:
            try:
                os.close(root_descriptor)
            except OSError:
                pass
