from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
import sqlite3
import struct
from contextlib import closing
from unittest.mock import patch

from capalith.onboarding import discover_sources, prepare_catalog
from capalith.settings import SettingsError, load_settings
from capalith.store import Store
from tests.helpers import make_skill


class RemainingSettingsTests(unittest.TestCase):
    def test_sources_replace_host_directories_and_storage_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "work"
            workspace.mkdir()
            home = root / "home"
            home.mkdir()
            custom = root / "custom"
            make_skill(custom / "library", "configured-host")
            make_skill(home / ".claude/skills", "previous-default")
            make_skill(root / "extra", "configured-extra")
            config = root / "settings.toml"
            config.write_text(
                f'[sources]\nextra = ["{root / "extra"}"]\n'
                f'[sources.hosts.claude]\nhome_default = "{custom}"\n'
                'user_dirs = ["library"]\nproject_dirs = []\n'
                f'[storage]\ndata_dir = "{root / "catalogs"}"\n'
                '[semantic]\nenabled = false\n'
            )
            config.chmod(0o600)
            with patch.dict(os.environ, {"HOME": str(home), "CAPALITH_CONFIG": str(config)}, clear=True):
                report = discover_sources(workspace)
                roots = [Path(source["path"]) for source in report["sources"]]
                self.assertEqual([custom / "library", root / "extra"], roots)
                database = prepare_catalog(roots)
                self.assertEqual(root / "catalogs", database.parent)
                self.assertEqual({"configured-host", "configured-extra"}, {
                    c["name"] for c in Store(database, read_only=True).discover("configured")["candidates"]
                })

    def test_folder_settings_refuse_parent_steps_nul_bytes_and_unknown_users(self):
        # Review of 25a1f68, notes 1 and 3: three folder settings, one rule, one message form.
        cases = (
            ("storage.data_dir", '[storage]\ndata_dir = "{v}"\n'),
            ("semantic.model.dir", '[semantic.model]\ndir = "{v}"\n'),
            ("ranking.cache_dir", '[ranking]\ncache_dir = "{v}"\n'),
        )
        bad = {"parent step": "/tmp/a/../b", "trailing parent step": "/tmp/a/..", "unknown user": "~no-such-user-capalith/x",
               "NUL byte": "/tmp/a\\u0000b", "relative": "rel/x"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for address, template in cases:
                for label, value in bad.items():
                    with self.subTest(address=address, case=label):
                        config = root / "settings.toml"
                        config.write_text("version = 1\n" + template.format(v=value))
                        config.chmod(0o600)
                        with patch.dict(os.environ, {"HOME": str(root), "CAPALITH_CONFIG": str(config)}, clear=True):
                            with self.assertRaises(SettingsError) as raised:
                                load_settings()
                        self.assertIn(address, str(raised.exception))
                        self.assertNotIn("no-such-user-capalith/x", str(raised.exception))
                good = root / "settings.toml"
                good.write_text("version = 1\n" + template.format(v=root / "ok" / "folder"))
                good.chmod(0o600)
                with patch.dict(os.environ, {"HOME": str(root), "CAPALITH_CONFIG": str(good)}, clear=True):
                    load_settings()

    def test_the_ranking_pointer_file_cache_folder_follows_the_same_rule(self):
        import json
        from capalith import ranking
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pointer = root / "ranking.json"
            pointer.write_text(json.dumps({"transport": "pkg.mod:fn", "cache_dir": str(root / "a" / ".." / "b")}))
            pointer.chmod(0o600)
            environment = {"HOME": str(root), "CAPALITH_RANKING_CONFIG": str(pointer)}
            with patch.dict(os.environ, environment, clear=True), self.assertRaises(ranking.RankingUnavailable) as raised:
                ranking.settings()
            self.assertEqual("configuration_error", str(raised.exception))
            # the same file with a plain folder is accepted: the refusal above is about the parent step alone
            pointer.write_text(json.dumps({"transport": "pkg.mod:fn", "cache_dir": str(root / "b")}))
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(root / "b" / "views.sqlite3", ranking.settings().cache)

    def test_config_show_reports_whether_calls_may_name_sources(self):
        from capalith.cli import _configuration_report
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "settings.toml"
            config.write_text("version = 1\n[security]\nallow_call_sources = false\n")
            config.chmod(0o600)
            with patch.dict(os.environ, {"HOME": str(root), "CAPALITH_CONFIG": str(config)}, clear=True):
                self.assertEqual({"allow_call_sources": False}, _configuration_report(None)["security"])
            with patch.dict(os.environ, {"HOME": str(root)}, clear=True):
                self.assertEqual({"allow_call_sources": True}, _configuration_report(None)["security"])

    def test_busy_timeout_applies_to_new_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "settings.toml"
            config.write_text('[storage]\nbusy_timeout_seconds = 0.025\n')
            config.chmod(0o600)
            with patch.dict(os.environ, {"CAPALITH_CONFIG": str(config)}, clear=True):
                store = Store(root / "catalog.sqlite3")
                store.initialize()
                connection = store._connect()
                try:
                    self.assertEqual(25, connection.execute("PRAGMA busy_timeout").fetchone()[0])
                finally:
                    connection.close()
                from capalith.store import _search_connection
                with _search_connection(Store(store.path, read_only=True)) as session:
                    self.assertEqual(25, session.connection.execute("PRAGMA busy_timeout").fetchone()[0])
                config.write_text('[storage]\nbusy_timeout_seconds = 0.075\n')
                with _search_connection(Store(store.path, read_only=True)) as session:
                    self.assertEqual(75, session.connection.execute("PRAGMA busy_timeout").fetchone()[0])

    def test_git_step_and_fetch_deadlines_reach_the_native_git_process(self):
        from capalith.intake import scan_git_source
        from tests.test_intake import git, make_git_remote
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, remote = make_git_remote(root)
            make_skill(work, "git-settings-marker")
            git("-C", work, "add", ".")
            git("-C", work, "commit", "-m", "add skill")
            git("-C", work, "push", "origin", "main")
            config = root / "settings.toml"
            config.write_text('[git]\ntimeout_seconds = 12.25\nfetch_timeout_seconds = 45.5\n[semantic]\nenabled = false\n')
            config.chmod(0o600)
            native_run = subprocess.run
            with patch.dict(os.environ, {"CAPALITH_CONFIG": str(config), "PATH": os.environ["PATH"]}, clear=True):
                store = Store(root/"catalog.sqlite3")
                store.initialize()
                source = store.add_git_source(remote.as_uri(), "refs/heads/main")
                with patch("capalith.intake.subprocess.run", wraps=native_run) as process:
                    scan_git_source(store, source.id)
                self.assertGreater(len(process.call_args_list), 1)
                for call in process.call_args_list:
                    self.assertEqual(45.5 if "fetch" in call.args[0] else 12.25, call.kwargs["timeout"])
                self.assertEqual("git-settings-marker", store.discover("git-settings-marker")["candidates"][0]["name"])

    def test_schema_seven_upgrade_preserves_vectors_and_search(self):
        from capalith.intake import scan_local_source
        vector = struct.pack("<384f", 1.0, *([0.0]*383))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_skill(root/"skills", "migration-marker")
            with patch.dict(os.environ, {"HOME":str(root),"XDG_CONFIG_HOME":str(root/"config")}, clear=True), \
                 patch("capalith.semantic.embed_documents", side_effect=lambda texts:tuple(vector for _ in texts)), \
                 patch("capalith.store._semantic_query_vector", return_value=vector):
                store = Store(root/"catalog.sqlite3")
                store.initialize()
                source = store.add_local_source(root/"skills")
                scan_local_source(store, source.id)
                with closing(sqlite3.connect(store.path)) as connection, connection:
                    before = connection.execute("SELECT * FROM catalog_semantic_vectors").fetchall()
                    self.assertEqual(1,len(before))
                    connection.execute("DROP TABLE catalog_semantic_vectors")
                    # Frozen schema-7 definition, independent of the new DDL.
                    connection.execute("""CREATE TABLE catalog_semantic_vectors(
                        artifact_id TEXT NOT NULL,
                        content_digest TEXT NOT NULL,
                        catalog_format_version INTEGER NOT NULL,
                        model_key TEXT NOT NULL,
                        projection_digest TEXT NOT NULL CHECK(length(projection_digest)=64),
                        dimensions INTEGER NOT NULL CHECK(dimensions=384),
                        vector BLOB NOT NULL CHECK(typeof(vector)='blob' AND length(vector)=1536),
                        PRIMARY KEY(artifact_id,content_digest,catalog_format_version,model_key),
                        FOREIGN KEY(artifact_id,content_digest,catalog_format_version)
                          REFERENCES catalog_entries(artifact_id,content_digest,catalog_format_version)
                          ON DELETE CASCADE
                    )""")
                    connection.executemany("INSERT INTO catalog_semantic_vectors VALUES(?,?,?,?,?,?,?)",before)
                    connection.execute("PRAGMA user_version=7")
                store.initialize()
                with closing(sqlite3.connect(store.path)) as connection:
                    self.assertEqual(8,connection.execute("PRAGMA user_version").fetchone()[0])
                    self.assertEqual(before,connection.execute("SELECT * FROM catalog_semantic_vectors").fetchall())
                    self.assertEqual([],connection.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual("used",store.discover("migration-marker")["semantic"]["status"])

    def test_user_endpoint_is_checked_even_without_a_paid_call(self):
        from capalith.ranking import load_policy
        from capalith._ranking_errors import RankingUnavailable
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.toml"
            with patch.dict(os.environ, {"CAPALITH_CONFIG": str(config)}, clear=True):
                config.write_text('[ranking]\nendpoint = "https://gateway.example.invalid/v1/systemone"\n')
                config.chmod(0o600)
                self.assertEqual("https://gateway.example.invalid/v1/systemone", load_policy()["typesafe"]["endpoint"])
                for endpoint in ("http://example.invalid/", "https://user:secret@example.invalid/", "https://example.invalid/?key=secret", "https://example.invalid/#secret", "https://example.invalid:wrong/"):
                    config.write_text(f'[ranking]\nendpoint = "{endpoint}"\n')
                    with self.assertRaises(RankingUnavailable):
                        load_policy()

    def test_unknown_ranking_key_is_rejected_at_load_and_permission_errors_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "settings.toml"
            config.write_text('[ranking]\napi_key = "do-not-print-this-value"\n')
            config.chmod(0o600)
            with patch.dict(os.environ, {"CAPALITH_CONFIG": str(config)}, clear=True):
                with self.assertRaises(SettingsError) as result:
                    load_settings()
                self.assertNotIn("do-not-print", str(result.exception))
                with patch.object(Path, "lstat", side_effect=PermissionError(str(config))):
                    with self.assertRaises(SettingsError) as result:
                        load_settings()
                # the message names the settings path on purpose (the user needs it); it never echoes the raw exception
                self.assertTrue(str(result.exception).startswith("settings_untrusted: file: " + str(config)))
                self.assertNotIn("PermissionError", str(result.exception))
                self.assertNotIn("Errno", str(result.exception))


if __name__ == "__main__":
    unittest.main()
