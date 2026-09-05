import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from capalith.identity import artifact_id, hash_bundle
from capalith.intake import ScanResult, scan_git_source, scan_local_source
from capalith.store import GitRevision, Store, StoreError
from tests.helpers import current_artifact, make_skill


def git(*arguments: object, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *(os.fspath(argument) for argument in arguments)],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def make_git_remote(root: Path) -> tuple[Path, Path]:
    work = root / "work"
    remote = root / "remote.git"
    git("init", "-b", "main", work)
    git("-C", work, "config", "user.name", "Capalith Tests")
    git("-C", work, "config", "user.email", "capalith@example.invalid")
    (work / "history.txt").write_text("initial\n", encoding="utf-8")
    git("-C", work, "add", "history.txt")
    git("-C", work, "commit", "-m", "initial")
    git("init", "--bare", remote)
    git("-C", work, "remote", "add", "origin", remote)
    git("-C", work, "push", "origin", "refs/heads/main:refs/heads/main")
    return work, remote


class IntakeTests(unittest.TestCase):
    def test_git_scan_preserves_committed_bytes_modes_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            work, remote = make_git_remote(temp)
            bundle_path = "skills/raw-blob"
            bundle = work / bundle_path
            (bundle / "bin").mkdir(parents=True)
            (bundle / "references").mkdir()
            (bundle / ".gitattributes").write_bytes(b"SKILL.md text eol=crlf\n")
            skill_bytes = (
                b"---\nname: raw-blob\ndescription: Raw Git blob fixture.\n---\n"
                b"# Raw blob\n"
            )
            (bundle / "SKILL.md").write_bytes(skill_bytes)
            executable_bytes = b"#!/bin/sh\nprintf 'raw blob\\n'\n"
            executable = bundle / "bin" / "run.sh"
            executable.write_bytes(executable_bytes)
            executable.chmod(0o755)
            binary_bytes = b"\x00\xffraw\r\nblob\x80\x00"
            (bundle / "references" / "data.bin").write_bytes(binary_bytes)
            git("-C", work, "add", bundle_path)
            git("-C", work, "commit", "-m", "add raw blob skill")
            commit = git("-C", work, "rev-parse", "HEAD").stdout.strip().decode()
            git("-C", work, "push", "origin", "refs/heads/main:refs/heads/main")

            store = Store(temp / "store.sqlite3")
            store.initialize()
            source = store.add_git_source(remote.as_uri(), "refs/heads/main")

            result, revision = scan_git_source(store, source.id)

            self.assertEqual(ScanResult(source.id, 1, 0), result)
            self.assertEqual(GitRevision("refs/heads/main", commit), revision)
            artifact = current_artifact(store, artifact_id(source.id, bundle_path))
            files = {item["path"]: item for item in artifact["manifest"]["files"]}
            self.assertEqual(
                {
                    ".gitattributes",
                    "SKILL.md",
                    "bin/run.sh",
                    "references/data.bin",
                },
                set(files),
            )
            self.assertEqual(
                f"sha256:{hashlib.sha256(skill_bytes).hexdigest()}",
                files["SKILL.md"]["sha256"],
            )
            self.assertEqual(
                f"sha256:{hashlib.sha256(binary_bytes).hexdigest()}",
                files["references/data.bin"]["sha256"],
            )
            self.assertTrue(files["bin/run.sh"]["executable"])
            self.assertEqual(commit, git("-C", remote, "rev-parse", "refs/heads/main").stdout.strip().decode())

    def test_git_scan_ignores_hostile_parent_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            _work, remote = make_git_remote(temp / "intended")
            outside_work, outside_remote = make_git_remote(temp / "outside")
            (outside_work / "history.txt").write_text("outside\n")
            git("-C", outside_work, "add", "history.txt")
            git("-C", outside_work, "commit", "-m", "outside")
            git("-C", outside_work, "push", "origin", "main")
            intended_commit = git("-C", remote, "rev-parse", "refs/heads/main").stdout.strip().decode()
            outside_commit = git("-C", outside_remote, "rev-parse", "refs/heads/main").stdout.strip().decode()
            self.assertNotEqual(intended_commit, outside_commit)

            hostile_state = temp / "hostile-state"
            hostile_state.mkdir()
            sentinel = hostile_state / "sentinel"
            sentinel.write_bytes(b"untouched")
            askpass_marker = temp / "askpass-used"
            ssh_marker = temp / "ssh-used"
            askpass = temp / "askpass"
            askpass.write_text(f"#!/bin/sh\ntouch '{askpass_marker}'\nexit 1\n")
            askpass.chmod(0o755)
            ssh = temp / "ssh"
            ssh.write_text(f"#!/bin/sh\ntouch '{ssh_marker}'\nexit 1\n")
            ssh.chmod(0o755)
            template = temp / "template"
            template.mkdir()
            (template / "copied-sentinel").write_bytes(b"must not be copied")
            hostile = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{outside_remote.as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": remote.as_uri(),
                "GIT_DIR": str(hostile_state),
                "GIT_WORK_TREE": str(hostile_state),
                "GIT_ASKPASS": str(askpass),
                "SSH_ASKPASS": str(askpass),
                "GIT_SSH_COMMAND": str(ssh),
                "GIT_TEMPLATE_DIR": str(template),
            }
            store = Store(temp / "store.sqlite3")
            store.initialize()
            source = store.add_git_source(remote.as_uri(), "refs/heads/main")

            with patch.dict(os.environ, hostile, clear=False):
                result, revision = scan_git_source(store, source.id)

            self.assertEqual(ScanResult(source.id, 0, 0), result)
            self.assertEqual(intended_commit, revision.resolved_commit)
            self.assertEqual(b"untouched", sentinel.read_bytes())
            self.assertFalse(askpass_marker.exists())
            self.assertFalse(ssh_marker.exists())

    def test_git_scan_rejects_unsafe_tree_paths_without_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            work, remote = make_git_remote(temp)
            object_id = git("-C", work, "rev-parse", "HEAD:history.txt").stdout.strip()

            def record(path: bytes) -> bytes:
                return b"100644 blob " + object_id + b"\t" + path + b"\0"

            valid = record(b"valid.txt")
            cases = (
                ("duplicate", valid + valid),
                ("prefix collision", valid + record(b"collision") + record(b"collision/child")),
                ("parent traversal", valid + record(b"../escape.txt")),
                ("absolute", valid + record(b"/absolute.txt")),
                ("invalid utf-8", valid + record(b"invalid-\xff.txt")),
            )
            store = Store(temp / "store.sqlite3")
            store.initialize()
            source = store.add_git_source(remote.as_uri(), "refs/heads/main")
            real_run = subprocess.run

            for label, unsafe_tree in cases:
                with self.subTest(label=label):
                    def replace_tree(arguments: list[str], **kwargs: Any):
                        if "ls-tree" in arguments:
                            return subprocess.CompletedProcess(
                                arguments,
                                0,
                                stdout=unsafe_tree,
                                stderr=b"raw-stderr-secret",
                            )
                        return real_run(arguments, **kwargs)

                    with patch("capalith.intake.subprocess.run", side_effect=replace_tree):
                        with self.assertRaises(StoreError) as caught:
                            scan_git_source(store, source.id)

                    self.assertEqual([], store.list_artifacts())
                    self.assertFalse((temp / "escape.txt").exists())
                    self.assertNotIn("raw-stderr-secret", str(caught.exception))
                    self.assertNotIn(remote.as_uri(), str(caught.exception))

    def test_git_scan_is_atomic_and_retains_prior_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            work, remote = make_git_remote(temp)
            bundle = make_skill(work / "skills", "alpha", b"version-a")
            git("-C", work, "add", bundle)
            git("-C", work, "commit", "-m", "add skill")
            commit_a = git("-C", work, "rev-parse", "HEAD").stdout.strip().decode()
            git("-C", work, "push", "origin", "refs/heads/main:refs/heads/main")

            store = Store(temp / "store.sqlite3")
            store.initialize()
            source = store.add_git_source(remote.as_uri(), "refs/heads/main")
            first_result, first_revision = scan_git_source(store, source.id)
            identifier = artifact_id(source.id, "skills/alpha")
            before_list = store.list_artifacts()
            before = current_artifact(store, identifier)

            (bundle / "references" / "data.bin").write_bytes(b"version-b")
            git("-C", work, "add", bundle)
            git("-C", work, "commit", "-m", "change skill")
            commit_b = git("-C", work, "rev-parse", "HEAD").stdout.strip().decode()
            git("-C", work, "push", "origin", "refs/heads/main:refs/heads/main")
            with sqlite3.connect(store.path) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_git_publication "
                    "BEFORE INSERT ON artifact_git_revisions "
                    "BEGIN SELECT RAISE(ABORT,'test rollback'); END"
                )

            with self.assertRaises(sqlite3.Error):
                scan_git_source(store, source.id)

            self.assertEqual(before_list, store.list_artifacts())
            self.assertEqual(before, current_artifact(store, identifier))
            with sqlite3.connect(store.path) as connection:
                connection.execute("DROP TRIGGER fail_git_publication")

            second_result, second_revision = scan_git_source(store, source.id)
            current = current_artifact(store, identifier)

            self.assertEqual(ScanResult(source.id, 1, 0), first_result)
            self.assertEqual(GitRevision("refs/heads/main", commit_a), first_revision)
            self.assertEqual(ScanResult(source.id, 1, 0), second_result)
            self.assertEqual(GitRevision("refs/heads/main", commit_b), second_revision)
            self.assertNotEqual(before["current_digest"], current["current_digest"])
            version_digests = {version["digest"] for version in current["versions"]}
            self.assertIn(before["current_digest"], version_digests)
            self.assertEqual(2, len(current["versions"]))
            revision_history = {
                (version["digest"], revision["resolved_commit"])
                for version in current["versions"]
                for revision in version.get("git_revisions", ())
            }
            self.assertIn((before["current_digest"], commit_a), revision_history)
            self.assertIn((current["current_digest"], commit_b), revision_history)

    def test_local_scan_preserves_bundle_identity_history_and_containment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source_root = temp / "source"
            parent = make_skill(source_root, "parent", b"parent-v1")
            nested = make_skill(parent, "nested", b"nested")
            beta = make_skill(source_root, "beta", b"beta-v1")
            outside = temp / "outside"
            make_skill(outside, "foreign", b"outside")
            (source_root / "unrelated-link").symlink_to(outside, target_is_directory=True)
            store = Store(temp / "store.sqlite3")
            store.initialize()
            source = store.add_local_source(source_root)

            self.assertEqual(ScanResult(source.id, 2, 0), scan_local_source(store, source.id))
            listed = store.list_artifacts()
            self.assertEqual(["beta", "parent"], [item["bundle_path"] for item in listed])
            parent_before = current_artifact(store, artifact_id(source.id, "parent"))
            beta_before = current_artifact(store, artifact_id(source.id, "beta"))
            self.assertIn(
                nested.joinpath("SKILL.md").relative_to(parent).as_posix(),
                {item["path"] for item in parent_before["manifest"]["files"]},
            )

            moved_root = temp / "moved" / "source"
            shutil.copytree(source_root, moved_root, symlinks=True)
            store.set_local_source_path(source.id, moved_root)
            self.assertEqual(ScanResult(source.id, 2, 0), scan_local_source(store, source.id))
            self.assertEqual(parent_before, current_artifact(store, parent_before["artifact_id"]))

            (moved_root / "parent" / "references" / "data.bin").write_bytes(b"parent-v2")
            shutil.rmtree(moved_root / "beta")
            self.assertEqual(ScanResult(source.id, 1, 1), scan_local_source(store, source.id))
            parent_after = current_artifact(store, parent_before["artifact_id"])
            beta_after = current_artifact(store, beta_before["artifact_id"])

            self.assertEqual(parent_before["artifact_id"], parent_after["artifact_id"])
            self.assertNotEqual(parent_before["current_digest"], parent_after["current_digest"])
            self.assertEqual(2, len(parent_after["versions"]))
            self.assertFalse(beta_after["present"])
            self.assertEqual(beta_before["manifest"], beta_after["manifest"])
            self.assertEqual(beta_before["versions"], beta_after["versions"])


if __name__ == "__main__":
    unittest.main()
