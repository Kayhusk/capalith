from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class SettingsTests(unittest.TestCase):
    def test_fifo_settings_are_refused_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config/capalith/settings.toml"
            config.parent.mkdir(parents=True, mode=0o700)
            os.mkfifo(config, 0o600)
            environment = {
                "PATH": os.environ["PATH"],
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            }
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "capalith",
                    "--db",
                    str(root / "catalog.sqlite3"),
                    "discover",
                    "documents",
                ],
                env=environment,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=3,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("settings_untrusted", result.stderr)

    def test_empty_xdg_uses_home_and_relative_xdg_cannot_load_project_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            project = root / "project"
            for parent, count in (
                (home / ".config", 2),
                (project, 1),
                (project / "relative", 1),
            ):
                config = parent / "capalith/settings.toml"
                config.parent.mkdir(parents=True, mode=0o700)
                config.write_text(f"[search]\ndefault_limit = {count}\n")
                config.chmod(0o600)
            script = (
                "import sys; from capalith.settings import load_settings, SettingsError\n"
                "try: print(load_settings()['search']['default_limit'])\n"
                "except SettingsError as error: print(str(error), file=sys.stderr); sys.exit(2)\n"
            )
            for value in ("", "relative"):
                with self.subTest(xdg=value):
                    environment = {
                        "PATH": os.environ["PATH"],
                        "HOME": str(home),
                        "XDG_CONFIG_HOME": value,
                        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                    }
                    result = subprocess.run(
                        [sys.executable, "-c", script],
                        cwd=project,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    if value:
                        self.assertEqual(2, result.returncode)
                        self.assertIn("settings_error", result.stderr)
                        self.assertNotIn(str(project), result.stderr)
                    else:
                        self.assertEqual(0, result.returncode, result.stderr)
                        self.assertEqual("2", result.stdout.strip())

    def test_index_batch_keeps_the_configuration_that_produced_its_vectors(self):
        from capalith.store import Store
        from capalith.intake import scan_local_source
        import struct

        vector = struct.pack("<384f", 1.0, *([0.0] * 383))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "skills"
            bundle = source_root / "document-helper"
            bundle.mkdir(parents=True)
            (bundle / "SKILL.md").write_text(
                "---\nname: document-helper\ndescription: Manage documents.\n---\n# Documents\n"
            )
            config = root / "settings.toml"

            def embed(texts):
                config.write_text("[semantic.projection]\nbody_chars = 128\n")
                config.chmod(0o600)
                return tuple(vector for _ in texts)

            with patch.dict(
                os.environ,
                {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")},
                clear=True,
            ):
                store = Store(root / "catalog.sqlite3")
                store.initialize()
                source = store.add_local_source(source_root)
                # A trusted user edits settings while indexing is in progress.
                # That must take effect on the next operation, never relabel
                # vectors made from the prior projection.
                os.environ["CAPALITH_CONFIG"] = str(config)
                config.write_text("version = 1\n")
                config.chmod(0o600)
                with (
                    patch("capalith.semantic.embed_documents", side_effect=embed),
                    patch("capalith.store._semantic_query_vector", return_value=vector),
                ):
                    scan_local_source(store, source.id)
                    self.assertEqual(
                        "stale", store.discover("documents")["semantic"]["status"]
                    )
                    config.write_text("version = 1\n")
                    self.assertEqual(
                        "used", store.discover("documents")["semantic"]["status"]
                    )

    def test_search_default_limit_respects_user_file(self):
        from capalith.store import Store
        from capalith.intake import scan_local_source
        from capalith.semantic import SemanticUnavailable

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "skills"
            for index in range(6):
                bundle = source_root / f"document-{index}"
                bundle.mkdir(parents=True)
                (bundle / "SKILL.md").write_text(
                    f"---\nname: document-{index}\ndescription: Manage documents.\n---\n# Documents\n"
                )
            config = root / "settings.toml"
            config.write_text("[search]\ndefault_limit = 1\n")
            config.chmod(0o600)
            with (
                patch.dict(os.environ, {"CAPALITH_CONFIG": str(config)}, clear=True),
                patch(
                    "capalith.semantic.embed_documents",
                    side_effect=SemanticUnavailable(),
                ),
            ):
                store = Store(root / "catalog.sqlite3")
                store.initialize()
                source = store.add_local_source(source_root)
                scan_local_source(store, source.id)
                result = store.discover("documents")
                self.assertEqual(1, result["retrieval"]["limit"])
                self.assertEqual(1, len(result["candidates"]))

    def test_plain_defaults_and_user_override_are_independent_of_working_directory(
        self,
    ):
        from capalith.settings import load_settings

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                os.environ,
                {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")},
                clear=True,
            ):
                self.assertEqual(5, load_settings()["search"]["default_limit"])
                config = root / "config/capalith/settings.toml"
                config.parent.mkdir(parents=True, mode=0o700)
                config.write_text("version = 1\n[search]\ndefault_limit = 1\n")
                config.chmod(0o600)
                self.assertEqual(1, load_settings()["search"]["default_limit"])
                config.unlink()
                self.assertEqual(5, load_settings()["search"]["default_limit"])

    def test_rejects_typos_invalid_limits_and_untrusted_files_without_echoing_values(
        self,
    ):
        from capalith.settings import SettingsError, load_settings

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "settings.toml"
            with patch.dict(os.environ, {"CAPALITH_CONFIG": str(config)}, clear=True):
                for text in (
                    "[search]\ndefalt_limit = 1",
                    "[search]\ndefault_limit = 0",
                    "[semantic.model]\ndimensions = true",
                ):
                    config.write_text(text)
                    config.chmod(0o600)
                    with self.assertRaises(SettingsError):
                        load_settings()
                config.write_text("version = 1")
                config.chmod(0o666)
                with self.assertRaisesRegex(SettingsError, "settings_untrusted"):
                    load_settings()
                config.chmod(0o600)
                link = root / "link.toml"
                link.symlink_to(config)
                os.environ["CAPALITH_CONFIG"] = str(link)
                with self.assertRaisesRegex(SettingsError, "settings_untrusted"):
                    load_settings()

    def test_projection_changes_semantic_identity_without_mutating_defaults(self):
        from capalith import semantic

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                os.environ,
                {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")},
                clear=True,
            ):
                original = semantic.model_key()
                config = root / "config/capalith/settings.toml"
                config.parent.mkdir(parents=True, mode=0o700)
                config.write_text("[semantic.projection]\nbody_chars = 128\n")
                config.chmod(0o600)
                self.assertNotEqual(original, semantic.model_key())
                self.assertNotEqual(
                    semantic.projection(
                        "sample", "description", "# Heading\n" + "x" * 200
                    ),
                    "sample\ndescription\n" + ("# Heading\n" + "x" * 200)[:64],
                )
                config.unlink()
                self.assertEqual(original, semantic.model_key())


if __name__ == "__main__":
    unittest.main()
