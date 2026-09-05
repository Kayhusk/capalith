from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capalith.store as store_module
from capalith.store import GitRevision, Store, StoreError


class StoreTests(unittest.TestCase):
    @staticmethod
    def _create_v1_database(database: Path) -> None:
        source_id = "src_00000000000000000000000000000001"
        digest = f"sha256:{'a' * 64}"
        manifest = (
            '{"byte_count":7,"digest":"'
            + digest
            + '","files":[{"executable":false,"path":"SKILL.md",'
            '"sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            '"size":7}]}'
        )
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE sources(
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind='local'),
                    locator TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE artifacts(
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    bundle_path TEXT NOT NULL,
                    current_digest TEXT NOT NULL,
                    present INTEGER NOT NULL CHECK(present IN(0,1)),
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(source_id,bundle_path)
                );
                CREATE TABLE artifact_versions(
                    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
                    digest TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    file_count INTEGER NOT NULL CHECK(file_count>=1),
                    byte_count INTEGER NOT NULL CHECK(byte_count>=0),
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY(artifact_id,digest)
                );
                PRAGMA user_version=1;
                """
            )
            connection.execute(
                "INSERT INTO sources VALUES(?,?,?,?,?)",
                (
                    source_id,
                    "local",
                    "/catalog",
                    "2026-01-01T01:02:03Z",
                    "2026-01-02T02:03:04Z",
                ),
            )
            connection.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?)",
                (
                    "art_00000000000000000000000000000001",
                    source_id,
                    "alpha",
                    digest,
                    1,
                    "2026-01-03T03:04:05Z",
                ),
            )
            connection.execute(
                "INSERT INTO artifact_versions VALUES(?,?,?,?,?,?)",
                (
                    "art_00000000000000000000000000000001",
                    digest,
                    manifest,
                    1,
                    7,
                    "2026-01-04T04:05:06Z",
                ),
            )

    def test_initialize_creates_usable_current_store_and_refuses_future_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            database = temp / "state" / "store.sqlite3"
            database.parent.mkdir()
            store = Store(database)

            store.initialize()
            store.initialize()

            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    store_module.SCHEMA_VERSION,
                    connection.execute("PRAGMA user_version").fetchone()[0],
                )
                self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='catalog_search_fts'"
                    ).fetchone()
                )

            source_root = temp / "source"
            source_root.mkdir()
            source = store.add_local_source(source_root)
            self.assertEqual([source], store.list_sources())

            future = temp / "future.sqlite3"
            with sqlite3.connect(future) as connection:
                connection.execute(
                    f"PRAGMA user_version={store_module.SCHEMA_VERSION + 1}"
                )
            with self.assertRaisesRegex(StoreError, r"^unsupported store schema version:"):
                Store(future).initialize()
            with sqlite3.connect(future) as connection:
                self.assertEqual(
                    store_module.SCHEMA_VERSION + 1,
                    connection.execute("PRAGMA user_version").fetchone()[0],
                )

    def test_v1_migration_preserves_inventory_without_reading_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "store.sqlite3"
            self._create_v1_database(database)

            with patch.object(
                Store,
                "validate_local_source",
                side_effect=AssertionError("migration read source bytes"),
            ):
                Store(database).initialize()

            store = Store(database)
            source = store.list_sources()[0]
            self.assertEqual("src_00000000000000000000000000000001", source.id)
            self.assertEqual("enabled", source.status)
            self.assertIsNone(source.requested_ref)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    store_module.SCHEMA_VERSION,
                    connection.execute("PRAGMA user_version").fetchone()[0],
                )
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
                self.assertEqual(
                    1,
                    connection.execute("SELECT COUNT(*) FROM artifact_versions").fetchone()[0],
                )
                self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_v6_migration_backfills_only_unambiguous_git_revision_pointers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "store.sqlite3"
            store = Store(database)
            store.initialize()
            single = store.add_git_source(
                "https://example.com/single.git", "refs/heads/main"
            )
            multiple = store.add_git_source(
                "https://example.com/multiple.git", "refs/heads/main"
            )
            single_revision = GitRevision("refs/heads/main", "a" * 40)
            older_revision = GitRevision("refs/heads/main", "b" * 40)
            current_revision = GitRevision("refs/heads/main", "c" * 40)
            store.apply_scan(single.id, (), single_revision)
            store.apply_scan(multiple.id, (), older_revision)
            store.apply_scan(multiple.id, (), current_revision)

            with sqlite3.connect(database) as connection:
                connection.execute("DROP TABLE current_git_revisions")
                connection.execute("PRAGMA user_version=6")

            store.initialize()

            self.assertEqual(single_revision, store.git_source_snapshot(single.id).revision)
            with self.assertRaisesRegex(
                StoreError,
                rf"^Git source requires a fresh scan: {multiple.id}$",
            ):
                store.git_source_snapshot(multiple.id)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM current_git_revisions"
                    ).fetchone()[0],
                )

            store.apply_scan(multiple.id, (), older_revision)
            self.assertEqual(older_revision, store.git_source_snapshot(multiple.id).revision)

    def test_failed_migration_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "store.sqlite3"
            self._create_v1_database(database)
            store = Store(database)

            with patch.object(
                store_module,
                "_SCHEMA",
                (store_module._SCHEMA[0], "CREATE TABLE"),
            ):
                with self.assertRaises(sqlite3.Error):
                    store.initialize()

            with sqlite3.connect(database) as connection:
                self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])

    def test_local_source_identity_and_status_survive_path_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            first_root = temp / "first"
            second_root = temp / "second"
            other_root = temp / "other"
            for root in (first_root, second_root, other_root):
                root.mkdir()
            database = temp / "state" / "store.sqlite3"
            database.parent.mkdir()
            store = Store(database)
            store.initialize()
            source = store.add_local_source(first_root)
            other = store.add_local_source(other_root)

            moved = store.set_local_source_path(source.id, second_root)
            self.assertEqual(source.id, moved.id)
            self.assertEqual(str(second_root.resolve()), moved.locator)
            self.assertEqual("disabled", store.set_source_status(source.id, "disabled").status)
            self.assertEqual("enabled", store.set_source_status(source.id, "enabled").status)
            self.assertEqual("removed", store.set_source_status(source.id, "removed").status)
            self.assertEqual("enabled", store.get_source(other.id).status)

            for action in (
                lambda: store.set_source_status(source.id, "enabled"),
                lambda: store.set_local_source_path(source.id, first_root),
                lambda: store.apply_scan(source.id, ()),
            ):
                with self.subTest(action=action):
                    with self.assertRaisesRegex(StoreError, rf"^source is removed: {source.id}$"):
                        action()

            contained = temp / "contained"
            contained.mkdir()
            contained_database = contained / "store.sqlite3"
            with self.assertRaises(StoreError):
                Store(contained_database).add_local_source(contained)
            self.assertFalse(contained_database.exists())

    def test_git_source_registration_validates_locator_and_ref_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            store = Store(temp / "store.sqlite3")
            store.initialize()

            accepted = (
                ("https://example.com/org/repo.git", "refs/heads/main"),
                (f"file://{temp}/repo.git", "refs/tags/v1.0"),
            )
            with patch("socket.create_connection") as network_request:
                sources = [store.add_git_source(*values) for values in accepted]
            network_request.assert_not_called()
            self.assertEqual([values[1] for values in accepted], [s.requested_ref for s in sources])

            invalid_locators = (
                "http://example.com/repo.git",
                "ssh://git@example.com/repo.git",
                "git@example.com:repo.git",
                "relative/repo.git",
                "https://user:secret@example.com/repo.git",
                "https://example.com/repo.git?token=secret",
                "https://example.com/repo.git#main",
                "file://server/absolute/repo.git",
            )
            for locator in invalid_locators:
                with self.subTest(locator=locator):
                    with self.assertRaisesRegex(StoreError, r"^invalid Git source URL$"):
                        store.add_git_source(locator, "refs/heads/main")

            invalid_refs = (
                "main",
                "--upload-pack=evil",
                "refs/remotes/origin/main",
                "refs/heads/",
                "refs/heads/main..backup",
                "refs/heads/main.lock",
                "refs/heads/main\x01",
            )
            for requested_ref in invalid_refs:
                with self.subTest(requested_ref=requested_ref):
                    with self.assertRaisesRegex(
                        StoreError, r"^invalid Git branch or tag reference$"
                    ):
                        store.add_git_source(
                            "https://example.com/org/repo.git", requested_ref
                        )

            self.assertEqual(len(accepted), len(store.list_sources()))
            replacement_root = temp / "replacement"
            replacement_root.mkdir()
            with self.assertRaisesRegex(StoreError, r"^source is not local:"):
                store.set_local_source_path(sources[0].id, replacement_root)

    def test_apply_scan_requires_matching_revision_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            store = Store(temp / "store.sqlite3")
            store.initialize()
            git_source = store.add_git_source(
                "https://example.com/org/repo.git", "refs/heads/main"
            )

            with self.assertRaisesRegex(
                StoreError, rf"^Git revision required for Git source: {git_source.id}$"
            ):
                store.apply_scan(git_source.id, ())
            with self.assertRaisesRegex(
                StoreError,
                rf"^Git revision does not match Git source: {git_source.id}$",
            ):
                store.apply_scan(
                    git_source.id,
                    (),
                    GitRevision("refs/tags/other", "a" * 40),
                )

            local_root = temp / "local"
            local_root.mkdir()
            local_source = store.add_local_source(local_root)
            with self.assertRaisesRegex(
                StoreError,
                rf"^Git revision forbidden for local source: {local_source.id}$",
            ):
                store.apply_scan(
                    local_source.id,
                    (),
                    GitRevision("refs/heads/main", "a" * 40),
                )


if __name__ == "__main__":
    unittest.main()
