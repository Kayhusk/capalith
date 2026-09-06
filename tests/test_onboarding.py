from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError
from mcp_types import TextResourceContents

from capalith.identity import BundleEntryError, hash_bundle
from capalith.onboarding import prepare_catalog
from capalith.store import Store, StoreError
from tests.helpers import make_skill
from tests.test_intake import git, make_git_remote


class OnboardingTests(unittest.TestCase):
    def test_agent_can_connect_index_and_refresh_without_server_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "skills"
            bundle = make_skill(source, "agent-first", b"first")
            before = hash_bundle(bundle)
            data = root / "data"

            async def exercise() -> None:
                parameters = StdioServerParameters(
                    command=sys.executable, args=["-m", "capalith.mcp_server"],
                    env={**os.environ, "HOME": str(root), "HERMES_HOME": str(root / "hermes"),
                         "XDG_DATA_HOME": str(data)},
                )
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        listed = {t.name: t for t in (await session.list_tools()).tools}
                        assert listed["index_skills"].annotations is not None
                        self.assertFalse(listed["index_skills"].annotations.read_only_hint)
                        report = await session.call_tool("config_show", {})
                        self.assertEqual("not_indexed", report.structured_content["catalog"]["state"])
                        self.assertFalse(data.exists())
                        missing = await session.call_tool("discover", {"query": "agent-first"})
                        self.assertTrue(missing.is_error)
                        indexed = await session.call_tool("index_skills", {"source_paths": [str(source)]})
                        self.assertFalse(indexed.is_error, indexed)
                        database = Path(indexed.structured_content["catalog"]["database_path"])
                        self.assertTrue(database.is_file())
                        self.assertEqual(before, hash_bundle(bundle))
                        for invalid in ({"source_paths": ["relative-skills"]}, {"workspace": "relative-workspace"}):
                            rejected = await session.call_tool("index_skills", invalid)
                            self.assertTrue(rejected.is_error)
                            retry = await session.call_tool("index_skills", {})
                            self.assertFalse(retry.is_error, retry)
                            self.assertEqual(str(database), retry.structured_content["catalog"]["database_path"])
                        first = await session.call_tool("discover", {"query": "agent-first"})
                        self.assertFalse(first.is_error)
                        original = first.structured_content["candidates"][0]
                        (bundle / "references/data.bin").write_bytes(b"second")
                        refreshed = await session.call_tool("index_skills", {"source_paths": [str(source)]})
                        self.assertFalse(refreshed.is_error)
                        second = await session.call_tool("discover", {"query": "agent-first"})
                        updated = second.structured_content["candidates"][0]
                        self.assertEqual(original["artifact_id"], updated["artifact_id"])
                        self.assertNotEqual(original["content_digest"], updated["content_digest"])
                        failed = await session.call_tool("index_skills", {"source_paths": [str(root / "missing")]})
                        self.assertTrue(failed.is_error)
                        unavailable = await session.call_tool("discover", {"query": "agent-first"})
                        self.assertTrue(unavailable.is_error)
                        self.assertEqual("index_required", unavailable.content[0].model_dump()["text"])
            anyio.run(exercise)

    def test_agent_automatically_indexes_host_locations_and_configured_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, workspace = root / "home", root / "workspace"
            sources = [home / ".agents/skills", root / "claude/skills",
                       root / "hermes/skills", root / "external",
                       workspace / ".claude/skills", workspace / ".agents/skills",
                       workspace / ".hermes/skills", root / "hermes/created"]
            bundles = [make_skill(source, f"host-{i}") for i, source in enumerate(sources)]
            before = [hash_bundle(bundle) for bundle in bundles]
            (workspace / ".git").mkdir()
            (root / "hermes/config.yaml").write_text(
                "skills:\n  create_dir: created\n  external_dirs:\n    - ${EXTRA_SKILLS}\n"
            )
            # Host roots and linked bundles resolve to explicit canonical intake roots.
            (home / ".codex").mkdir()
            (home / ".codex/skills").symlink_to(sources[0], target_is_directory=True)
            linked = make_skill(root / "linked", "linked-skill")
            category = root / "category"
            categorized = make_skill(category, "categorized-skill")
            (sources[0] / "category").symlink_to(category, target_is_directory=True)
            (category / "nested").mkdir()
            (category / "nested/linked-skill").symlink_to(linked, target_is_directory=True)
            (category / "cycle").symlink_to(sources[0], target_is_directory=True)
            (category / "broken").symlink_to(root / "missing", target_is_directory=True)
            make_skill(root / "unrelated", "not-selected")

            async def exercise() -> None:
                parameters = StdioServerParameters(
                    command=sys.executable, args=["-m", "capalith.mcp_server"], cwd=str(workspace),
                    env={**os.environ, "PYTHONPATH": str(Path.cwd()), "HOME": str(home),
                         "HERMES_HOME": str(root / "hermes"), "CLAUDE_CONFIG_DIR": str(root / "claude"),
                         "CODEX_HOME": str(home / ".codex"), "EXTRA_SKILLS": "../external",
                         "XDG_DATA_HOME": str(root / "data")},
                )
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        status = await session.call_tool("config_show", {})
                        self.assertFalse(status.is_error, status)
                        self.assertFalse((root / "data").exists())
                        indexed = await session.call_tool("index_skills", {})
                        self.assertFalse(indexed.is_error, indexed)
                        content = indexed.structured_content
                        self.assertEqual({str(p) for p in sources} | {str(category), str(linked)},
                                         {s["locator"] for s in content["sources"]})
                        self.assertEqual(len(sources) + 2, len(content["sources"]))
                        self.assertEqual([], content["source_discovery"]["warnings"])
                        report = await session.call_tool("discover", {"query": "host", "limit": 20})
                        self.assertEqual(len(sources), len(report.structured_content["candidates"]))
                        absent = await session.call_tool("discover", {"query": "not-selected"})
                        self.assertEqual([], absent.structured_content["candidates"])
                        for skill in (linked, categorized):
                            match = await session.call_tool("discover", {"query": skill.name})
                            self.assertEqual(skill.name, match.structured_content["candidates"][0]["name"])
            anyio.run(exercise)
            self.assertEqual(before, [hash_bundle(bundle) for bundle in bundles])

    def test_agent_context_overrides_server_cwd_and_survives_project_switch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server_cwd, first, second = [root / p for p in ("installation", "first", "second")]
            home, profile = root / "home", root / "custom-profile"
            profile_skill = make_skill(profile / "created", "profile-marker")
            profile.joinpath("config.yaml").write_text("skills:\n  create_dir: created\n")
            decoy = make_skill(home / ".hermes/skills", "wrong-profile")
            homes = {"hermes": str(profile), "claude": str(root / "custom-claude"),
                     "codex": str(root / "custom-codex")}
            other_roots = [Path(homes[host]) / "skills" for host in ("claude", "codex")]
            for source in other_roots:
                make_skill(source, source.parent.name + "-marker")
            make_skill(server_cwd / ".agents/skills", "wrong-workspace")
            for workspace in (first, second):
                make_skill(workspace / ".agents/skills", workspace.name + "-marker")
            before = hash_bundle(profile_skill)

            async def exercise() -> None:
                parameters = StdioServerParameters(
                    command=sys.executable, args=["-m", "capalith.mcp_server"], cwd=str(server_cwd),
                    env={"PATH": os.environ["PATH"], "PYTHONPATH": str(Path.cwd()),
                         "HOME": str(home), "XDG_DATA_HOME": str(root / "data")},
                )
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        for workspace in (first, second):
                            arguments: dict[str, object] = {"workspace": str(workspace)}
                            if workspace == first:
                                arguments["host_homes"] = homes
                            indexed = await session.call_tool("index_skills", arguments)
                            self.assertFalse(indexed.is_error, indexed)
                            result = indexed.structured_content
                            self.assertEqual(str(workspace), result["source_discovery"]["workspace"])
                            self.assertEqual(homes, result["source_discovery"]["host_homes"])
                            self.assertEqual(
                                {str(profile / "created"), str(workspace / ".agents/skills"),
                                 *(str(p) for p in other_roots)},
                                {s["locator"] for s in result["sources"]},
                            )
                            found = await session.call_tool("discover", {"query": "marker"})
                            self.assertEqual({"profile-marker", workspace.name + "-marker",
                                              "custom-claude-marker", "custom-codex-marker"},
                                             {c["name"] for c in found.structured_content["candidates"]})
                            database = result["catalog"]["database_path"]
                            rejected = await session.call_tool("index_skills", {
                                "host_homes": {"hermes": "relative-profile"},
                            })
                            self.assertTrue(rejected.is_error)
                            refreshed = await session.call_tool("index_skills", {})
                            self.assertFalse(refreshed.is_error, refreshed)
                            self.assertEqual(database, refreshed.structured_content["catalog"]["database_path"])
                        stale = await session.call_tool("discover", {"query": "first"})
                        self.assertEqual([], stale.structured_content["candidates"])
            anyio.run(exercise)
            self.assertEqual(before, hash_bundle(profile_skill))
            self.assertTrue(decoy.is_dir())

    def test_native_project_context_precedes_server_cwd_but_not_explicit_workspace(self) -> None:
        from capalith.onboarding import discover_sources

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native, explicit = root / "native", root / "explicit"
            for workspace in (native, explicit):
                make_skill(workspace / ".agents/skills", workspace.name)
            with patch.dict(os.environ, {"HOME": str(root), "HERMES_HOME": str(root / "hermes"),
                                         "CLAUDE_CONFIG_DIR": str(root / "claude"),
                                         "CODEX_HOME": str(root / "codex"),
                                         "CLAUDE_PROJECT_DIR": str(native)}):
                self.assertEqual(str(native), discover_sources()["workspace"])
                self.assertEqual(str(explicit), discover_sources(explicit)["workspace"])

    def test_discovery_does_not_promote_bundle_support_links_to_sources(self) -> None:
        from capalith.onboarding import discover_sources

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = make_skill(root / "source", "primary")
            outside = make_skill(root / "outside", "support-not-installed")
            (bundle / "references/other").symlink_to(outside, target_is_directory=True)
            (bundle / "other").symlink_to(outside, target_is_directory=True)
            (root / ".agents").mkdir()
            (root / ".agents/skills").symlink_to(bundle, target_is_directory=True)
            with patch.dict(os.environ, {"HOME": str(root), "HERMES_HOME": str(root / "hermes"),
                                         "CLAUDE_CONFIG_DIR": str(root / "claude"),
                                         "CODEX_HOME": str(root / "codex")}):
                for marker_is_directory in (False, True):
                    if marker_is_directory:
                        (bundle / "SKILL.md").unlink()
                        (bundle / "SKILL.md").mkdir()
                    with self.subTest(marker_is_directory=marker_is_directory):
                        found = discover_sources(root)
                        sources = found["sources"]
                        assert isinstance(sources, list)
                        self.assertEqual([str(bundle)], [s["path"] for s in sources])

    def test_setup_directory_swaps_do_not_write_selected_sources(self) -> None:
        for phase in ("mkdir", "lock"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                selected = root / "selected"
                bundle = make_skill(selected)
                before = hash_bundle(bundle)
                paths = sorted(p.relative_to(bundle) for p in bundle.rglob("*"))
                data = root / "data"
                data.mkdir()
                swapped = False
                original_open, original_mkdir = os.open, os.mkdir

                def swap() -> None:
                    nonlocal swapped
                    target = data if phase == "mkdir" else data / "capalith/catalogs"
                    target.rename(root / "parked")
                    target.symlink_to(bundle, target_is_directory=True)
                    swapped = True

                def open_at(path, *args, **kwargs):
                    if phase == "lock" and str(path).endswith(".lock") and not swapped:
                        swap()
                    return original_open(path, *args, **kwargs)

                def mkdir_at(path, *args, **kwargs):
                    if phase == "mkdir" and Path(path).name == "capalith" and not swapped:
                        swap()
                    return original_mkdir(path, *args, **kwargs)

                descriptors = set(os.listdir("/proc/self/fd"))
                with patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}), \
                     patch("os.open", side_effect=open_at), \
                     patch("os.mkdir", side_effect=mkdir_at):
                    with self.assertRaises((OSError, StoreError)):
                        prepare_catalog([selected])
                self.assertTrue(swapped)
                self.assertEqual(descriptors, set(os.listdir("/proc/self/fd")))
                self.assertEqual(paths, sorted(p.relative_to(bundle) for p in bundle.rglob("*")))
                self.assertEqual(before, hash_bundle(bundle))

    def test_failed_refresh_preserves_the_previous_source_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "selected"
            bundle = make_skill(selected)
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data")}):
                database = prepare_catalog([selected])
                before = database.read_bytes()
                unsafe = bundle / "references" / "escape"
                unsafe.symlink_to(root / "outside")
                with self.assertRaises(BundleEntryError):
                    prepare_catalog([selected])
                self.assertEqual(before, database.read_bytes())
                unsafe.unlink()
                self.assertEqual(database, prepare_catalog([selected]))
                self.assertEqual(1, len(Store(database, read_only=True).list_sources()))

    def test_simultaneous_connections_share_registration_and_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            make_skill(first, "shared-name", b"first")
            make_skill(second, "shared-name", b"second")
            data = root / "data"
            command = [
                sys.executable, "-m", "capalith.mcp_server",
                "--source", str(first), "--source", str(second), "--source", str(first),
            ]
            with ExitStack() as stack:
                processes = [stack.enter_context(subprocess.Popen(
                    command, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env={**os.environ, "XDG_DATA_HOME": str(data)},
                )) for _ in range(2)]
                for process in processes:
                    stdout, stderr = process.communicate(timeout=20)
                    self.assertEqual(0, process.returncode, stderr.decode())
                    self.assertEqual(b"", stdout)
            databases = list(data.rglob("*.sqlite3"))
            self.assertEqual(1, len(databases))
            store = Store(databases[0], read_only=True)
            sources = store.list_sources()
            self.assertEqual(2, len(sources))
            candidates = store.discover("shared-name")["candidates"]
            assert isinstance(candidates, list)
            self.assertEqual(1, len(candidates))
            winner = next(s for s in sources if s.id == candidates[0]["source_id"])
            self.assertEqual(str(first), winner.locator)

    def test_managed_catalog_refuses_unselected_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "selected"
            other = root / "other"
            make_skill(selected, "selected-skill")
            make_skill(other, "other-skill")
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data")}):
                database = prepare_catalog([selected])
                Store(database).add_local_source(other)
                before = database.read_bytes()
                with self.assertRaises(StoreError):
                    prepare_catalog([selected])
                self.assertEqual(before, database.read_bytes())

    def test_bad_setup_is_redacted_and_does_not_write_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            skill = make_skill(root / "skills")
            before = hash_bundle(skill)
            existing = root / "existing.sqlite3"
            existing.write_bytes(b"operator owned")
            cases = [
                (["--source", str(root / "missing")], 1),
                (["--source", str(root)], 1),
                (["--git", "https://user:secret@example.invalid/repo", "refs/heads/main"], 1),
                (["--git", "https://example.invalid/repo", "main"], 1),
                (["--db", str(existing), "--source", str(skill)], 2),

            ]
            for args, expected_code in cases:
                with self.subTest(args=args):
                    result = subprocess.run(
                        [sys.executable, "-m", "capalith.mcp_server", *args],
                        input=b"", capture_output=True,
                        env={**os.environ, "XDG_DATA_HOME": str(data)}, timeout=10,
                    )
                    self.assertEqual(expected_code, result.returncode)
                    if expected_code == 1:
                        self.assertIn(b"catalog_setup_failed", result.stderr)
                    self.assertNotIn(b"secret", result.stderr)
                    self.assertEqual(b"", result.stdout)
                    self.assertFalse(data.exists())
            self.assertEqual(before, hash_bundle(skill))
            self.assertEqual(b"operator owned", existing.read_bytes())

    def test_git_connection_indexes_selected_ref_without_manual_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, remote = make_git_remote(root)
            make_skill(work, "git-onboarding")
            git("-C", work, "add", ".")
            git("-C", work, "commit", "-m", "add skill")
            git("-C", work, "push", "origin", "main")
            data = root / "data"
            result = subprocess.run(
                [sys.executable, "-m", "capalith.mcp_server", "--git", remote.as_uri(), "refs/heads/main"],
                input=b"", capture_output=True,
                env={**os.environ, "XDG_DATA_HOME": str(data)}, timeout=20,
            )
            self.assertEqual(0, result.returncode, result.stderr.decode())
            databases = list(data.rglob("*.sqlite3"))
            self.assertEqual(1, len(databases))
            store = Store(databases[0], read_only=True)
            candidates = store.discover("git-onboarding")["candidates"]
            assert isinstance(candidates, list)
            self.assertEqual(["git-onboarding"], [c["name"] for c in candidates])
            source = store.list_sources()[0]
            self.assertEqual("refs/heads/main", source.requested_ref)
            revision = store.git_source_snapshot(source.id).revision
            assert revision is not None
            self.assertEqual(
                git("-C", work, "rev-parse", "HEAD").stdout.strip().decode(),
                revision.resolved_commit,
            )

    def test_bundled_guidance_is_available_without_host_skill_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "missing.sqlite3"

            async def exercise() -> None:
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "capalith.mcp_server", "--db", str(database)],
                )
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams) as session:
                        initialized = await session.initialize()
                        self.assertIn("Call only the tools the task needs.", initialized.instructions or "")
                        resources = await session.list_resources()
                        self.assertEqual(
                            {"capalith://guide/SKILL.md", "capalith://guide/references/operations.md"},
                            {resource.uri for resource in resources.resources},
                        )
                        for resource in resources.resources:
                            relative = resource.uri.removeprefix("capalith://guide/")
                            expected = files("capalith").joinpath("skills/capalith", relative).read_text(encoding="utf-8")
                            result = await session.read_resource(resource.uri)
                            content = result.contents[0]
                            assert isinstance(content, TextResourceContents)
                            self.assertEqual(expected, content.text)
                        with self.assertRaises(MCPError):
                            await session.read_resource("capalith://guide/../../store.py")
            anyio.run(exercise)
            self.assertFalse(database.exists())

    def test_source_connection_prepares_and_refreshes_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "skills"
            bundle = make_skill(source, "onboarding-demo", b"first version")
            before = hash_bundle(bundle)
            data = root / "data"
            environment = {**os.environ, "XDG_DATA_HOME": str(data)}
            args = ["-m", "capalith.mcp_server", "--source", str(source)]
            started = subprocess.run(
                [sys.executable, *args], input=b"", capture_output=True,
                env=environment, timeout=20,
            )
            self.assertEqual(0, started.returncode, started.stderr.decode())
            self.assertEqual(b"", started.stdout)

            async def search() -> tuple[str, str, Path]:
                parameters = StdioServerParameters(
                    command=sys.executable, args=args, env=environment,
                )
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        report = await session.call_tool("config_show", {})
                        self.assertFalse(report.is_error)
                        config = report.structured_content
                        assert config is not None
                        self.assertEqual(1, len(config["sources"]))
                        self.assertEqual("server_startup", config["automatic_behavior"]["source_scan"])
                        database = Path(config["catalog"]["database_path"])
                        self.assertTrue(database.is_relative_to(data))
                        stored_before = database.read_bytes()
                        result = await session.call_tool("discover", {"query": "onboarding-demo"})
                        self.assertFalse(result.is_error)
                        assert result.structured_content is not None
                        candidates = result.structured_content["candidates"]
                        self.assertEqual(["onboarding-demo"], [item["name"] for item in candidates])
                        self.assertEqual(stored_before, database.read_bytes())
                        match = candidates[0]
                        return match["artifact_id"], match["content_digest"], database

            first_id, first_digest, database = anyio.run(search)
            self.assertEqual(before, hash_bundle(bundle))
            (bundle / "references" / "data.bin").write_bytes(b"second version")
            second_id, second_digest, second_database = anyio.run(search)
            self.assertEqual(database, second_database)
            self.assertEqual(first_id, second_id)
            self.assertNotEqual(first_digest, second_digest)
            self.assertEqual(1, len(Store(database, read_only=True).list_sources()))


if __name__ == "__main__":
    unittest.main()
