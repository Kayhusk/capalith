from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from capalith import intake
from capalith.cli import main
from capalith.store import Store
from tests.helpers import make_skill


def invoke(database: Path, *arguments: str | os.PathLike[str]) -> tuple[int, Any, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    status = main(
        ["--db", str(database), *(os.fspath(value) for value in arguments)],
        stdout=stdout,
        stderr=stderr,
    )
    payload = json.loads(stdout.getvalue()) if stdout.getvalue() else None
    return status, payload, stderr.getvalue()


def succeed(test: unittest.TestCase, database: Path, *arguments: str | os.PathLike[str]) -> Any:
    status, payload, error = invoke(database, *arguments)
    test.assertEqual("", error)
    test.assertEqual(0, status)
    return payload


def snapshot_database_state(database: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        sorted(
            (path.name, path.read_bytes())
            for path in database.parent.glob(f"{database.name}*")
        )
    )


def git(*arguments: str | os.PathLike[str]) -> bytes:
    return subprocess.run(
        ["git", *(os.fspath(argument) for argument in arguments)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def make_git_remote(root: Path) -> tuple[Path, Path, str]:
    work = root / "work"
    remote = root / "remote.git"
    git("init", "-b", "main", work)
    git("-C", work, "config", "user.name", "Capalith Tests")
    git("-C", work, "config", "user.email", "capalith@example.invalid")
    make_skill(work / "skills", "cli-git", b"git-version")
    git("-C", work, "add", "skills")
    git("-C", work, "commit", "-m", "add CLI Git skill")
    commit = git("-C", work, "rev-parse", "HEAD").strip().decode()
    git("init", "--bare", remote)
    git("-C", work, "remote", "add", "origin", remote)
    git("-C", work, "push", "origin", "refs/heads/main:refs/heads/main")
    return work, remote, commit


def make_related_skill(root: Path, name: str, requires: tuple[str, ...] = ()) -> Path:
    bundle = root / name
    bundle.mkdir(parents=True)
    relationship = (
        "metadata:\n  capalith.requires: " + ", ".join(requires) + "\n"
        if requires
        else ""
    )
    (bundle / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Common route skill.\n{relationship}---\n"
        "Common route instructions.\n",
        encoding="utf-8",
    )
    return bundle


class CliTests(unittest.TestCase):
    def test_local_workflow_returns_json_and_stored_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "source"
            bundle = make_skill(root)
            guide = bundle / "references" / "guide.md"
            guide.write_text("Stored guide.\n", encoding="utf-8")
            database = temp / "state" / "capalith.sqlite3"

            source = succeed(self, database, "source", "add-local", root)
            source_id = source["source_id"]
            self.assertEqual("enabled", source["status"])
            self.assertEqual(1, succeed(self, database, "scan", source_id)["observed"])

            result = succeed(self, database, "discover", "demo")
            candidate = result["candidates"][0]
            self.assertEqual("demo", candidate["name"])
            shown = succeed(
                self,
                database,
                "artifact",
                "show",
                candidate["artifact_id"],
                candidate["content_digest"],
            )
            self.assertEqual(candidate["artifact_id"], shown["artifact_id"])
            self.assertFalse(any("text" in resource for resource in shown["resources"]))
            resource = succeed(
                self,
                database,
                "artifact",
                "show",
                candidate["artifact_id"],
                candidate["content_digest"],
                "references/guide.md",
            )
            self.assertEqual("Stored guide.\n", resource["resource"]["text"])
            self.assertNotIn("versions", resource)
            self.assertNotIn("manifest", resource)

            moved = temp / "moved"
            root.rename(moved)
            updated = succeed(self, database, "source", "set-path", source_id, moved)
            self.assertEqual(source_id, updated["source_id"])
            self.assertEqual(1, succeed(self, database, "scan", source_id)["observed"])
            self.assertEqual(
                candidate["artifact_id"],
                succeed(self, database, "discover", "demo")["candidates"][0][
                    "artifact_id"
                ],
            )

    def test_source_status_and_config_commands_preserve_owned_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "source"
            make_skill(root)
            database = temp / "state" / "capalith.sqlite3"

            config = succeed(self, database, "config", "show")
            self.assertFalse(database.exists())
            self.assertEqual([], config["sources"])
            self.assertEqual("unavailable", config["capabilities"]["host_integration"])

            source = succeed(self, database, "source", "add-local", root)
            source_id = source["source_id"]
            succeed(self, database, "scan", source_id)
            candidate = succeed(self, database, "discover", "demo")["candidates"][0]

            disabled = succeed(self, database, "source", "disable", source_id)
            self.assertEqual("disabled", disabled["status"])
            self.assertEqual([], succeed(self, database, "discover", "demo")["candidates"])

            enabled = succeed(self, database, "source", "enable", source_id)
            self.assertEqual("enabled", enabled["status"])
            self.assertEqual(
                candidate["artifact_id"],
                succeed(self, database, "discover", "demo")["candidates"][0][
                    "artifact_id"
                ],
            )

            removed = succeed(self, database, "source", "remove", source_id)
            self.assertEqual("removed", removed["status"])
            self.assertEqual([], succeed(self, database, "discover", "demo")["candidates"])
            self.assertEqual("removed", succeed(self, database, "source", "list")[0]["status"])
            status, payload, error = invoke(database, "source", "enable", source_id)
            self.assertEqual((1, None), (status, payload))
            self.assertIn("is removed", error)
            self.assertEqual(1, len(succeed(self, database, "artifact", "list")))

    def test_discovery_and_traversal_flags_continue_stable_views(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            root = temp / "source"
            make_related_skill(root, "alpha", ("beta", "gamma"))
            make_related_skill(root, "beta")
            make_related_skill(root, "gamma")
            database = temp / "capalith.sqlite3"
            source = succeed(self, database, "source", "add-local", root)
            source_id = source["source_id"]
            succeed(self, database, "scan", source_id)

            full = succeed(
                self,
                database,
                "--source-id",
                source_id,
                "--limit",
                "3",
                "discover",
                "common route",
            )
            first = succeed(
                self,
                database,
                "--source-id",
                source_id,
                "--limit",
                "1",
                "discover",
                "common route",
            )
            second = succeed(
                self,
                database,
                "--source-id",
                source_id,
                "--limit",
                "2",
                "--offset",
                str(first["retrieval"]["next_offset"]),
                "--view-id",
                first["retrieval"]["view_id"],
                "discover",
                "common route",
            )
            self.assertEqual(
                [item["artifact_id"] for item in full["candidates"]],
                [item["artifact_id"] for item in first["candidates"] + second["candidates"]],
            )

            alpha = next(
                candidate for candidate in full["candidates"] if candidate["name"] == "alpha"
            )
            traversal = succeed(
                self,
                database,
                "--source-id",
                source_id,
                "--relationship-type",
                "requires",
                "--direction",
                "both",
                "--depth",
                "2",
                "--limit",
                "1",
                "traverse",
                alpha["artifact_id"],
                alpha["content_digest"],
            )
            self.assertTrue(traversal["has_more"])
            continued = succeed(
                self,
                database,
                "--source-id",
                source_id,
                "--relationship-type",
                "requires",
                "--direction",
                "both",
                "--depth",
                "2",
                "--limit",
                "10",
                "--offset",
                str(traversal["next_offset"]),
                "--view-id",
                traversal["view_id"],
                "traverse",
                alpha["artifact_id"],
                alpha["content_digest"],
            )
            self.assertEqual(
                {"beta", "gamma"},
                {
                    edge["target_name"]
                    for edge in traversal["relationships"] + continued["relationships"]
                },
            )

    def test_git_workflow_reports_the_exact_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            work, remote, commit = make_git_remote(temp)
            database = temp / "capalith.sqlite3"
            source = succeed(
                self,
                database,
                "source",
                "add-git",
                remote.as_uri(),
                "refs/heads/main",
            )
            scanned = succeed(self, database, "scan", source["source_id"])
            self.assertEqual("refs/heads/main", scanned["requested_ref"])
            self.assertEqual(commit, scanned["resolved_commit"])
            candidate = succeed(self, database, "discover", "git-version")["candidates"][0]
            shown = succeed(
                self,
                database,
                "artifact",
                "show",
                candidate["artifact_id"],
                candidate["content_digest"],
            )
            self.assertEqual(
                commit,
                shown["versions"][0]["git_revisions"][0]["resolved_commit"],
            )
            self.assertTrue((work / "skills" / "cli-git" / "SKILL.md").is_file())

    def test_git_source_review_reports_drift_read_only_from_explicit_scan_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            work, remote, _initial_commit = make_git_remote(temp)
            make_skill(work / "skills", "obsolete", b"stored")
            git("-C", work, "add", "skills")
            git("-C", work, "commit", "-m", "add stored skill")
            stored_commit = git("-C", work, "rev-parse", "HEAD").strip().decode()
            git("-C", work, "push", "origin", "refs/heads/main:refs/heads/main")

            database = temp / "capalith.sqlite3"
            source = succeed(
                self,
                database,
                "source",
                "add-git",
                remote.as_uri(),
                "refs/heads/main",
            )
            source_id = source["source_id"]
            before_review = snapshot_database_state(database)
            self.assertEqual(
                {
                    "source_id": source_id,
                    "requested_ref": "refs/heads/main",
                    "stored_commit": None,
                    "observed_commit": stored_commit,
                    "current": False,
                    "added": ["skills/cli-git", "skills/obsolete"],
                    "changed": [],
                    "absent": [],
                },
                succeed(self, database, "source", "review", source_id),
            )
            self.assertEqual(before_review, snapshot_database_state(database))

            succeed(self, database, "scan", source_id)
            before_review = snapshot_database_state(database)
            self.assertEqual(
                {
                    "source_id": source_id,
                    "requested_ref": "refs/heads/main",
                    "stored_commit": stored_commit,
                    "observed_commit": stored_commit,
                    "current": True,
                    "added": [],
                    "changed": [],
                    "absent": [],
                },
                succeed(self, database, "source", "review", source_id),
            )
            self.assertEqual(before_review, snapshot_database_state(database))

            (work / "skills" / "cli-git" / "references" / "data.bin").write_bytes(
                b"newer"
            )
            make_skill(work / "skills", "future-only", b"future")
            git("-C", work, "add", "skills")
            git("-C", work, "commit", "-m", "newer commit")
            newer_commit = git("-C", work, "rev-parse", "HEAD").strip().decode()
            git("-C", work, "push", "origin", "refs/heads/main:refs/heads/main")

            timestamp = "2099-01-01T00:00:00Z"
            with patch("capalith.store._utc_timestamp", return_value=timestamp):
                succeed(self, database, "scan", source_id)
                git("-C", work, "reset", "--hard", stored_commit)
                git(
                    "-C",
                    work,
                    "push",
                    "--force",
                    "origin",
                    "refs/heads/main:refs/heads/main",
                )
                succeed(self, database, "scan", source_id)

            with sqlite3.connect(database) as connection:
                revisions = {
                    commit: (identifier, last_seen_at)
                    for identifier, commit, last_seen_at in connection.execute(
                        "SELECT id,resolved_commit,last_seen_at FROM git_revisions"
                    )
                }
            self.assertLess(revisions[stored_commit][0], revisions[newer_commit][0])
            self.assertEqual(timestamp, revisions[stored_commit][1])
            self.assertEqual(timestamp, revisions[newer_commit][1])
            before_review = snapshot_database_state(database)
            self.assertEqual(
                {
                    "source_id": source_id,
                    "requested_ref": "refs/heads/main",
                    "stored_commit": stored_commit,
                    "observed_commit": stored_commit,
                    "current": True,
                    "added": [],
                    "changed": [],
                    "absent": [],
                },
                succeed(self, database, "source", "review", source_id),
            )
            self.assertEqual(before_review, snapshot_database_state(database))

            (work / "skills" / "cli-git" / "references" / "data.bin").write_bytes(
                b"changed"
            )
            git("-C", work, "rm", "-r", "skills/obsolete")
            make_skill(work / "skills", "added", b"added")
            git("-C", work, "add", "skills")
            git("-C", work, "commit", "-m", "drift")
            observed_commit = git("-C", work, "rev-parse", "HEAD").strip().decode()
            git("-C", work, "push", "origin", "refs/heads/main:refs/heads/main")

            before_review = snapshot_database_state(database)
            self.assertEqual(
                {
                    "source_id": source_id,
                    "requested_ref": "refs/heads/main",
                    "stored_commit": stored_commit,
                    "observed_commit": observed_commit,
                    "current": False,
                    "added": ["skills/added"],
                    "changed": ["skills/cli-git"],
                    "absent": ["skills/obsolete"],
                },
                succeed(self, database, "source", "review", source_id),
            )
            self.assertEqual(before_review, snapshot_database_state(database))

    def test_git_source_review_rejects_source_disabled_after_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            _work, remote, _commit = make_git_remote(temp)
            database = temp / "capalith.sqlite3"
            source = succeed(
                self,
                database,
                "source",
                "add-git",
                remote.as_uri(),
                "refs/heads/main",
            )
            source_id = source["source_id"]
            real_stage = intake._stage_git_source
            staging_ran = False

            def disable_after_staging(source_to_stage: Any) -> Any:
                nonlocal staging_ran
                result = real_stage(source_to_stage)
                staging_ran = True
                Store(database).set_source_status(source_id, "disabled")
                return result

            with patch(
                "capalith.intake._stage_git_source",
                side_effect=disable_after_staging,
            ):
                self.assertEqual(
                    (1, None, f"source is disabled: {source_id}\n"),
                    invoke(database, "source", "review", source_id),
                )
            self.assertTrue(staging_ran)

    def test_git_source_review_rejects_ineligible_sources_before_remote_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local_root = temp / "local"
            make_skill(local_root)
            database = temp / "capalith.sqlite3"
            local = succeed(self, database, "source", "add-local", local_root)
            disabled = succeed(
                self,
                database,
                "source",
                "add-git",
                "https://example.com/disabled.git",
                "refs/heads/main",
            )
            succeed(self, database, "source", "disable", disabled["source_id"])
            removed = succeed(
                self,
                database,
                "source",
                "add-git",
                "https://example.com/removed.git",
                "refs/heads/main",
            )
            succeed(self, database, "source", "remove", removed["source_id"])
            cases = (
                (local["source_id"], f"source is not Git: {local['source_id']}\n"),
                (
                    disabled["source_id"],
                    f"source is disabled: {disabled['source_id']}\n",
                ),
                (removed["source_id"], f"source is removed: {removed['source_id']}\n"),
                ("src_missing", "unknown source: src_missing\n"),
            )
            with patch(
                "capalith.intake._stage_git_source",
                side_effect=AssertionError("remote access attempted"),
            ) as stage:
                for source_id, expected_error in cases:
                    with self.subTest(source_id=source_id):
                        before_review = snapshot_database_state(database)
                        self.assertEqual(
                            (1, None, expected_error),
                            invoke(database, "source", "review", source_id),
                        )
                        self.assertEqual(
                            before_review, snapshot_database_state(database)
                        )
            stage.assert_not_called()

    def test_unsafe_git_registration_does_not_create_state(self) -> None:
        cases = (
            ("https://user:secret@example.com/repo.git", "refs/heads/main"),
            ("https://example.com/repo.git?secret", "refs/heads/main"),
            ("https://example.com/repo.git#secret", "refs/heads/main"),
            ("ssh://example.com/repo.git", "refs/heads/main"),
            ("https://example.com/repo.git", "main"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            for index, (locator, requested_ref) in enumerate(cases):
                with self.subTest(locator=locator, requested_ref=requested_ref):
                    database = temp / f"state-{index}.sqlite3"
                    status, payload, error = invoke(
                        database,
                        "source",
                        "add-git",
                        locator,
                        requested_ref,
                    )
                    self.assertEqual((1, None), (status, payload))
                    self.assertIn("invalid Git", error)
                    self.assertFalse(database.exists())
                    self.assertNotIn("secret", error)

    def test_source_review_missing_database_error_does_not_leak_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "private-review-marker" / "capalith.sqlite3"

            result = invoke(database, "source", "review", "src_missing")

            self.assertEqual((1, None, "database does not exist\n"), result)
            self.assertNotIn("private-review-marker", result[2])
            self.assertNotIn(str(database), result[2])
            self.assertFalse(database.exists())

    def test_errors_are_fixed_and_do_not_leak_database_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            database = temp / "state" / "capalith.sqlite3"
            status, payload, error = invoke(database, "discover")
            self.assertEqual((1, None, "discovery query must contain a word\n"), (status, payload, error))

            source = temp / "source"
            source.mkdir()
            with patch("capalith.cli.Store.initialize", side_effect=sqlite3.DatabaseError("private SQL")):
                status, payload, error = invoke(database, "source", "add-local", source)
            self.assertEqual((1, None, "database operation failed\n"), (status, payload, error))


if __name__ == "__main__":
    unittest.main()
