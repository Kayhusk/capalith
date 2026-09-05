from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from capalith.catalog import CatalogError, extract_catalog
from capalith.identity import (
    BundleEntryError,
    _DIRECTORY_OPEN_FLAGS,
    _Metadata,
    _hash_bundle_descriptor,
    _list_directory,
    _metadata,
    _require_directory,
    artifact_id,
)
from capalith.store import ArtifactObservation, GitRevision, Source, Store, StoreError

_OpenBundle = tuple[str, int, _Metadata]
_Discovery = tuple[Path, list[int], tuple[_OpenBundle, ...]]
_GIT_PREFIX = (
    "git",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "protocol.file.allow=always",
    "-c",
    "credential.helper=",
    "-c",
    "core.hooksPath=/dev/null",
)


@dataclass(frozen=True)
class ScanResult:
    source_id: str
    observed: int
    absent: int


@dataclass(frozen=True)
class GitReviewReport:
    source_id: str
    requested_ref: str
    stored_commit: str | None
    observed_commit: str
    current: bool
    added: tuple[str, ...]
    changed: tuple[str, ...]
    absent: tuple[str, ...]


def _observe_bundle(
    source_id: str,
    bundle_path: str,
    root: Path,
    descriptor: int,
    root_metadata: _Metadata,
) -> ArtifactObservation:
    bundle = root if bundle_path == "." else root / bundle_path
    manifest = _hash_bundle_descriptor(bundle, descriptor, root_metadata)
    return ArtifactObservation(
        artifact_id(source_id, bundle_path),
        bundle_path,
        manifest,
        extract_catalog(descriptor, manifest),
    )


def _run_git(
    arguments: list[str],
    environment: dict[str, str],
    timeout: int,
    message: str,
    *,
    stdout_descriptor: int | None = None,
) -> bytes:
    try:
        result = subprocess.run(
            [*_GIT_PREFIX, *arguments],
            check=False,
            stdout=(
                subprocess.PIPE if stdout_descriptor is None else stdout_descriptor
            ),
            stderr=subprocess.PIPE,
            env=environment,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StoreError(message) from error
    if result.returncode != 0:
        raise StoreError(message)
    return b"" if stdout_descriptor is not None else result.stdout


def _stage_git_source(
    source: Source,
) -> tuple[GitRevision, tuple[ArtifactObservation, ...]]:
    requested_ref = source.requested_ref
    if (
        source.kind != "git"
        or requested_ref is None
        or not requested_ref.startswith(("refs/heads/", "refs/tags/"))
    ):
        raise StoreError("could not validate Git branch or tag reference")

    with tempfile.TemporaryDirectory(prefix="capalith-git-") as temp_dir:
        staging = Path(temp_dir)
        home = staging / "home"
        xdg_config = staging / "xdg-config"
        git_template = staging / "git-template"
        process_temp = staging / "tmp"
        for directory in (home, xdg_config, git_template, process_temp):
            directory.mkdir()
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C",
            "LC_ALL": "C",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(xdg_config),
            "TMPDIR": str(process_temp),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_COUNT": "0",
            "GIT_TEMPLATE_DIR": str(git_template),
            "GIT_PROTOCOL_FROM_USER": "0",
        }
        _run_git(
            ["check-ref-format", requested_ref],
            environment,
            10,
            "could not validate Git branch or tag reference",
        )
        advertisement = _run_git(
            ["ls-remote", "--refs", "--exit-code", source.locator, requested_ref],
            environment,
            10,
            "could not look up Git branch or tag reference",
        )
        expected_suffix = b"\t" + requested_ref.encode("utf-8") + b"\n"
        advertised_oid = advertisement[: -len(expected_suffix)]
        if (
            not advertisement.endswith(expected_suffix)
            or len(advertised_oid) not in (40, 64)
            or re.fullmatch(rb"[0-9A-Fa-f]+", advertised_oid) is None
        ):
            raise StoreError("could not look up Git branch or tag reference")
        object_format = "sha1" if len(advertised_oid) == 40 else "sha256"

        repository = staging / "repo.git"
        _run_git(
            ["init", "--bare", f"--object-format={object_format}", str(repository)],
            environment,
            10,
            "could not initialize temporary Git repository",
        )
        _run_git(
            [
                "--git-dir",
                str(repository),
                "fetch",
                "--depth=1",
                "--no-tags",
                "--recurse-submodules=no",
                "--no-write-fetch-head",
                source.locator,
                f"{requested_ref}:refs/capalith/input",
            ],
            environment,
            60,
            "could not fetch Git branch or tag reference",
        )
        resolved = _run_git(
            [
                "--git-dir",
                str(repository),
                "rev-parse",
                "--verify",
                "--end-of-options",
                "refs/capalith/input^{commit}",
            ],
            environment,
            10,
            "could not resolve fetched Git commit",
        )
        if re.fullmatch(rb"[0-9A-Fa-f]+\n", resolved) is None or len(resolved) != len(
            advertised_oid
        ) + 1:
            raise StoreError("could not resolve fetched Git commit")
        resolved_commit = resolved[:-1].decode("ascii").lower()

        tree = _run_git(
            [
                "--git-dir",
                str(repository),
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                "--format=%(objectmode) %(objecttype) %(objectname)%x09%(path)",
                resolved_commit,
            ],
            environment,
            10,
            "could not inspect fetched Git files",
        )
        tree_records: list[tuple[bytes, bytes, tuple[str, ...]]] = []
        tree_paths: set[tuple[str, ...]] = set()
        expected_object_types = {
            b"100644": b"blob",
            b"100755": b"blob",
            b"120000": b"blob",
            b"160000": b"commit",
        }
        if tree:
            if not tree.endswith(b"\0"):
                raise StoreError("could not inspect fetched Git files")
            for record in tree[:-1].split(b"\0"):
                try:
                    header, raw_path = record.split(b"\t", 1)
                    mode, object_type, object_id = header.split(b" ")
                except ValueError as error:
                    raise StoreError("could not inspect fetched Git files") from error
                if (
                    not raw_path
                    or not mode
                    or not object_type
                    or len(object_id) != len(resolved_commit)
                    or re.fullmatch(rb"[0-9A-Fa-f]+", object_id) is None
                    or mode not in expected_object_types
                    or object_type != expected_object_types[mode]
                ):
                    raise StoreError("could not inspect fetched Git files")
                try:
                    path_parts = tuple(raw_path.decode("utf-8").split("/"))
                except UnicodeDecodeError as error:
                    raise StoreError("could not inspect fetched Git files") from error
                if (
                    any(part in ("", ".", "..") for part in path_parts)
                    or path_parts in tree_paths
                ):
                    raise StoreError("could not inspect fetched Git files")
                tree_paths.add(path_parts)
                tree_records.append((mode, object_id, path_parts))

        if any(
            path_parts[:index] in tree_paths
            for path_parts in tree_paths
            for index in range(1, len(path_parts))
        ):
            raise StoreError("could not inspect fetched Git files")

        unsupported_paths = tuple(
            path_parts
            for mode, _object_id, path_parts in tree_records
            if mode in (b"120000", b"160000")
        )
        if any(path_parts[-1] == "SKILL.md" for path_parts in unsupported_paths):
            raise StoreError("Git skill contains a symbolic link or submodule")

        materialized = staging / "materialized"
        materialized_descriptors: list[int] = []
        try:
            staging_descriptor = os.open(staging, _DIRECTORY_OPEN_FLAGS)
            materialized_descriptors.append(staging_descriptor)
            os.mkdir("materialized", 0o700, dir_fd=staging_descriptor)
            materialized_descriptor = os.open(
                "materialized", _DIRECTORY_OPEN_FLAGS, dir_fd=staging_descriptor
            )
            materialized_descriptors.append(materialized_descriptor)
            if not stat.S_ISDIR(os.fstat(materialized_descriptor).st_mode):
                raise StoreError("could not read fetched Git file")
            directories: dict[tuple[str, ...], int] = {
                (): materialized_descriptor
            }

            for mode, object_id, path_parts in tree_records:
                if mode not in (b"100644", b"100755"):
                    continue

                parent_parts = path_parts[:-1]
                for index, part in enumerate(parent_parts, start=1):
                    key = parent_parts[:index]
                    if key in directories:
                        continue
                    parent_descriptor = directories[key[:-1]]
                    os.mkdir(part, 0o700, dir_fd=parent_descriptor)
                    directory_descriptor = os.open(
                        part,
                        _DIRECTORY_OPEN_FLAGS,
                        dir_fd=parent_descriptor,
                    )
                    materialized_descriptors.append(directory_descriptor)
                    if not stat.S_ISDIR(os.fstat(directory_descriptor).st_mode):
                        raise StoreError("could not read fetched Git file")
                    directories[key] = directory_descriptor

                parent_descriptor = directories[parent_parts]
                file_descriptor = os.open(
                    path_parts[-1],
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                try:
                    if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                        raise StoreError("could not read fetched Git file")
                    _run_git(
                        [
                            "--git-dir",
                            str(repository),
                            "cat-file",
                            "blob",
                            object_id.decode("ascii"),
                        ],
                        environment,
                        10,
                        "could not read fetched Git file",
                        stdout_descriptor=file_descriptor,
                    )
                    os.fchmod(
                        file_descriptor, 0o755 if mode == b"100755" else 0o644
                    )
                finally:
                    os.close(file_descriptor)
        except StoreError:
            raise
        except OSError as error:
            raise StoreError("could not read fetched Git file") from error
        finally:
            _close_descriptors(materialized_descriptors)

        root, descriptors, bundles = _discover(materialized)
        try:
            for bundle_path, _descriptor, _root_metadata in bundles:
                bundle_parts = () if bundle_path == "." else tuple(bundle_path.split("/"))
                if any(
                    len(path_parts) > len(bundle_parts)
                    and path_parts[: len(bundle_parts)] == bundle_parts
                    for path_parts in unsupported_paths
                ):
                    raise StoreError("Git skill contains a symbolic link or submodule")
            observations = tuple(
                _observe_bundle(
                    source.id,
                    bundle_path,
                    root,
                    descriptor,
                    root_metadata,
                )
                for bundle_path, descriptor, root_metadata in bundles
            )
            _verify_bundle_paths(root, descriptors[0], bundles)
        finally:
            _close_descriptors(descriptors)
        return GitRevision(requested_ref, resolved_commit), observations


def _encoded(value: str, path: Path) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise BundleEntryError(f"path is not valid UTF-8: {path}") from error


def _close_descriptors(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _discover(source_root: Path) -> _Discovery:
    supplied = Path(source_root)
    descriptors: list[int] = []
    try:
        supplied_stat = os.lstat(supplied)
        if stat.S_ISLNK(supplied_stat.st_mode):
            raise BundleEntryError(f"local source directory cannot be a symlink: {supplied}")
        if not stat.S_ISDIR(supplied_stat.st_mode):
            raise BundleEntryError(
                f"local source directory must be an existing directory: {supplied}"
            )

        root = supplied.resolve(strict=True)
        _encoded(os.fspath(root), root)
        root_descriptor = os.open(root, _DIRECTORY_OPEN_FLAGS)
        descriptors.append(root_descriptor)
        root_stat = os.fstat(root_descriptor)
        _require_directory(root, root_stat)
        if (
            supplied_stat.st_dev,
            supplied_stat.st_ino,
            stat.S_IFMT(supplied_stat.st_mode),
        ) != (
            root_stat.st_dev,
            root_stat.st_ino,
            stat.S_IFMT(root_stat.st_mode),
        ):
            raise BundleEntryError(
                f"local source directory changed while being discovered: {supplied}"
            )
        root_metadata = _metadata(root_stat)
        root_path_stat = os.stat(root, follow_symlinks=False)
        _require_directory(root, root_path_stat)
        if root_metadata != _metadata(root_path_stat):
            raise BundleEntryError(
                f"local source directory changed while being discovered: {supplied}"
            )

        directories: list[_OpenBundle] = [(".", root_descriptor, root_metadata)]
        bundles: list[_OpenBundle] = []
        directory_index = 0
        while directory_index < len(directories):
            relative, descriptor, initial_metadata = directories[directory_index]
            directory = root if relative == "." else root / relative
            entries = _list_directory(directory, descriptor)

            if any(name == "SKILL.md" for name, _encoded_name, _entry in entries):
                bundles.append((relative, descriptor, initial_metadata))
            else:
                for name, _encoded_name, entry_stat in entries:
                    if name.startswith(".") or not stat.S_ISDIR(entry_stat.st_mode):
                        continue
                    child = name if relative == "." else f"{relative}/{name}"
                    _encoded(child, root / child)
                    child_descriptor = os.open(
                        name, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor
                    )
                    descriptors.append(child_descriptor)
                    child_stat = os.fstat(child_descriptor)
                    _require_directory(root / child, child_stat)
                    if _metadata(entry_stat) != _metadata(child_stat):
                        raise BundleEntryError(
                            f"source directory changed while being discovered: "
                            f"{root / child}"
                        )
                    directories.append(
                        (child, child_descriptor, _metadata(child_stat))
                    )

            final_stat = os.fstat(descriptor)
            _require_directory(directory, final_stat)
            if initial_metadata != _metadata(final_stat):
                raise BundleEntryError(
                    f"source directory changed while being discovered: {directory}"
                )
            directory_index += 1

        bundles.sort(key=lambda bundle: bundle[0].encode("utf-8"))
        return root, descriptors, tuple(bundles)
    except BundleEntryError:
        _close_descriptors(descriptors)
        raise
    except (OSError, ValueError) as error:
        _close_descriptors(descriptors)
        raise BundleEntryError(f"could not inspect local source directory: {supplied}") from error


def discover_bundle_roots(source_root: Path) -> tuple[Path, ...]:
    root, descriptors, bundles = _discover(source_root)
    try:
        return tuple(
            root if relative == "." else root / relative
            for relative, _descriptor, _metadata_value in bundles
        )
    finally:
        _close_descriptors(descriptors)


def _verify_bundle_paths(
    root: Path,
    source_descriptor: int,
    bundles: tuple[_OpenBundle, ...],
) -> None:
    opened: list[int] = []
    try:
        source_stat = os.fstat(source_descriptor)
        source_path_stat = os.lstat(root)
        _require_directory(root, source_path_stat)
        if _metadata(source_stat) != _metadata(source_path_stat):
            raise BundleEntryError(
                f"local source directory changed while being scanned: {root}"
            )

        for relative, _descriptor, expected_metadata in bundles:
            descriptor = source_descriptor
            try:
                for part in () if relative == "." else relative.split("/"):
                    descriptor = os.open(
                        part,
                        _DIRECTORY_OPEN_FLAGS,
                        dir_fd=descriptor,
                    )
                    opened.append(descriptor)
                final_stat = os.fstat(descriptor)
                _require_directory(root / relative, final_stat)
                if expected_metadata != _metadata(final_stat):
                    raise BundleEntryError(
                        f"skill path changed during scan: {root / relative}"
                    )
            finally:
                _close_descriptors(opened)
                opened.clear()
    except BundleEntryError:
        raise
    except OSError as error:
        raise BundleEntryError(
            f"source namespace changed while being scanned: {root}"
        ) from error


def scan_local_source(store: Store, source_id: str) -> ScanResult:
    source = store.get_source(source_id, require_enabled=True)
    root, descriptors, bundles = _discover(Path(source.locator))
    try:
        observations = tuple(
            _observe_bundle(
                source_id,
                bundle_path,
                root,
                descriptor,
                root_metadata,
            )
            for bundle_path, descriptor, root_metadata in bundles
        )
        _verify_bundle_paths(root, descriptors[0], bundles)
    finally:
        _close_descriptors(descriptors)

    absent = store.apply_scan(source_id, observations)
    return ScanResult(source_id, len(observations), absent)


def scan_git_source(
    store: Store, source_id: str
) -> tuple[ScanResult, GitRevision]:
    source = store.get_source(source_id, require_enabled=True)
    try:
        revision, observations = _stage_git_source(source)
    except (BundleEntryError, CatalogError) as error:
        raise StoreError("could not inspect skill files from Git source") from error
    absent = store.apply_scan(source_id, observations, revision)
    return ScanResult(source_id, len(observations), absent), revision


def review_git_source(store: Store, source_id: str) -> GitReviewReport:
    source = store.git_source_snapshot(source_id).source
    try:
        observed_revision, observations = _stage_git_source(source)
    except (BundleEntryError, CatalogError) as error:
        raise StoreError("could not inspect skill files from Git source") from error

    stored = store.git_source_snapshot(source_id)
    if stored.source != source or observed_revision.requested_ref != source.requested_ref:
        raise StoreError(f"source changed during review: {source_id}")
    stored_artifacts = dict(stored.artifacts)
    observed_artifacts = {
        observation.bundle_path: observation.manifest.digest
        for observation in observations
    }
    added = tuple(sorted(observed_artifacts.keys() - stored_artifacts.keys()))
    changed = tuple(
        sorted(
            path
            for path in observed_artifacts.keys() & stored_artifacts.keys()
            if observed_artifacts[path] != stored_artifacts[path]
        )
    )
    absent = tuple(sorted(stored_artifacts.keys() - observed_artifacts.keys()))
    stored_commit = (
        stored.revision.resolved_commit if stored.revision is not None else None
    )
    current = (
        stored_commit == observed_revision.resolved_commit
        and not added
        and not changed
        and not absent
    )
    return GitReviewReport(
        source_id,
        observed_revision.requested_ref,
        stored_commit,
        observed_revision.resolved_commit,
        current,
        added,
        changed,
        absent,
    )
