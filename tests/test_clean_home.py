from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def skill(root: Path, name: str, description: str) -> None:
    bundle = root / name
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n# Instructions\n{description}\n")


class CleanHomeTests(unittest.TestCase):
    def test_clean_home_defaults_live_override_and_untrusted_settings_through_mcp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("home", "config", "data", "cache", "work"):
                (root / name).mkdir()
            sources = root / "home/.claude/skills"
            skill(sources,"pdf-form-filler","Fill in and flatten PDF forms.")
            skill(sources,"spreadsheet-pivot","Build pivot tables from spreadsheet data.")
            for index in range(5):
                skill(sources,f"pdf-helper-{index}","Help with PDF documents and forms.")
            environment = {"PATH":os.environ["PATH"], "HOME":str(root/"home"), "XDG_CONFIG_HOME":str(root/"config"), "XDG_DATA_HOME":str(root/"data"), "XDG_CACHE_HOME":str(root/"cache")}
            self.assertEqual({"PATH","HOME","XDG_CONFIG_HOME","XDG_DATA_HOME","XDG_CACHE_HOME"},set(environment))
            config = root / "config/capalith/settings.toml"
            async def exercise():
                params = StdioServerParameters(command=sys.executable,args=["-B","-m","capalith.mcp_server"],cwd=PROJECT_ROOT,env=environment)
                async with stdio_client(params) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        async def discover(query="fill in a pdf form"):
                            response = await session.call_tool("discover", {"query":query,"workspace":str(root/"work")})
                            self.assertFalse(response.is_error)
                            return response.structured_content
                        plain = await discover()
                        self.assertEqual("pdf-form-filler",plain["candidates"][0]["name"])
                        self.assertEqual(3,plain["retrieval"]["limit"])  # the short answer lists three by default
                        self.assertEqual(3,len(plain["candidates"]))
                        self.assertTrue(list((root/"data/capalith/catalogs").glob("*.sqlite3")))
                        report = (await session.call_tool("config_show",{})).structured_content
                        self.assertTrue(any("user:claude" in source["origins"] for source in report["source_discovery"]["sources"]))
                        config.parent.mkdir(mode=0o700)
                        config.write_text("[search]\ndefault_limit = 1\n")
                        config.chmod(0o600)
                        changed = await discover()
                        self.assertEqual(1,changed["retrieval"]["limit"])
                        self.assertEqual(1,len(changed["candidates"]))
                        config.unlink()
                        self.assertEqual(3,(await discover())["retrieval"]["limit"])
                        skill(root/"team-skills", "team-document-helper", "Manage the team document archive.")
                        config.write_text(f'[sources]\nextra = ["{root / "team-skills"}"]\n')
                        config.chmod(0o600)
                        self.assertEqual("team-document-helper", (await discover("team archive"))["candidates"][0]["name"])
                        report = (await session.call_tool("config_show",{})).structured_content
                        self.assertTrue(any("extra" in source["origins"] for source in report["source_discovery"]["sources"]))
                        config.write_text(f'[storage]\ndata_dir = "{root / "custom-catalogs"}"\n')
                        await discover()
                        report = (await session.call_tool("config_show",{})).structured_content
                        self.assertEqual(root/"custom-catalogs", Path(report["catalog"]["database_path"]).parent)
                        self.assertFalse(any(c["name"] == "team-document-helper" for c in (await discover("team archive"))["candidates"]))
                        for text in ("[search]\ndefalt_limit = 1", "[search]\ndefault_limit = 0"):
                            config.write_text(text)
                            config.chmod(0o600)
                            error = await session.call_tool("discover",{"query":"pdf"})
                            self.assertTrue(error.is_error)
                            self.assertIn("settings_error",error.content[0].text)
                        config.write_text("version = 1")
                        config.chmod(0o666)
                        error = await session.call_tool("config_show",{})
                        self.assertTrue(error.is_error)
                        self.assertIn("settings_untrusted",error.content[0].text)
                        config.chmod(0o600)
                        saved = config.with_suffix(".saved")
                        config.rename(saved)
                        config.symlink_to(saved)
                        error = await session.call_tool("discover",{"query":"pdf"})
                        self.assertTrue(error.is_error)
                        self.assertIn("settings_untrusted",error.content[0].text)
            anyio.run(exercise)

    def test_project_files_and_call_arguments_cannot_override_user_trust_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "work"
            workspace.mkdir()
            skill(root/"home/.claude/skills", "native-marker", "Native marker instructions.")
            skill(root/"agent-skills", "agent-marker", "Agent marker instructions.")
            config = root/"config/capalith/settings.toml"
            config.parent.mkdir(parents=True,mode=0o700)
            # These project-local files are data, never configuration sources.
            for name in ("settings.toml", "capalith.toml", ".capalith.toml"):
                (workspace/name).write_text(
                    '[ranking]\nendpoint = "https://untrusted.invalid/steal"\nkey_env = "OTHER_SECRET"\n'
                    f'[sources]\nextra = ["{root / "agent-skills"}"]\n'
                    '[semantic.model]\nid = "untrusted/model"\nrevision = "untrusted"\n'
                )
            environment={"PATH":os.environ["PATH"],"HOME":str(root/"home"),
                         "XDG_CONFIG_HOME":str(root/"config"),"XDG_DATA_HOME":str(root/"data"),"XDG_CACHE_HOME":str(root/"cache"),
                         "PYTHONPATH":str(PROJECT_ROOT)}
            async def exercise():
                params=StdioServerParameters(command=sys.executable,args=["-B","-m","capalith.mcp_server"],cwd=workspace,env=environment)
                async with stdio_client(params) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        response=await session.call_tool("discover",{"query":"marker","workspace":str(workspace)})
                        self.assertFalse(response.is_error,response)
                        self.assertEqual(["native-marker"],[c["name"] for c in response.structured_content["candidates"]])
                        # Today's default permits an explicit source selection.
                        response=await session.call_tool("discover",{"query":"marker","source_paths":[str(root/"agent-skills")]})
                        self.assertEqual(["agent-marker"],[c["name"] for c in response.structured_content["candidates"]])
                        config.write_text('[security]\nallow_call_sources = false\n')
                        config.chmod(0o600)
                        for supplied in ({}, {"source_paths":[str(root/"agent-skills")]},
                                         {"extra_source_paths":[str(root/"agent-skills")]},
                                         {"host_homes":{"claude":str(root/"agent-skills")}},
                                         {"workspace":str(root/"agent-skills")}):
                            response=await session.call_tool("discover",{"query":"marker",**supplied})
                            self.assertFalse(response.is_error,response)
                            self.assertEqual(["native-marker"],[c["name"] for c in response.structured_content["candidates"]])
                        for supplied in ({"endpoint":"https://untrusted.invalid/steal"},{"key_env":"OTHER_SECRET"},
                                         {"model":"untrusted/model"},{"settings":{"security":{"allow_call_sources":True}}}):
                            response=await session.call_tool("discover",{"query":"marker",**supplied})
                            self.assertTrue(response.is_error)
                            self.assertEqual("invalid_request",response.content[0].text)
            anyio.run(exercise)
            from capalith.ranking import load_policy
            from capalith.settings import load_settings
            with patch.dict(os.environ,environment,clear=True):
                self.assertEqual("https://api.typesafe.ai/v1/systemone",load_policy()["typesafe"]["endpoint"])
                self.assertEqual("TYPESAFE_API_KEY",load_policy()["typesafe"]["key_env"])
                self.assertEqual("BAAI/bge-small-en-v1.5",load_settings()["semantic"]["model"]["id"])
                self.assertEqual([],load_settings()["sources"]["extra"])

    def test_changed_projection_is_stale_until_reindexed_and_old_vectors_are_removed(self):
        from capalith import semantic
        from capalith.intake import scan_local_source
        from capalith.store import Store
        vector = struct.pack("<384f",1.0,*([0.0]*383))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "skills"
            skill(source_root,"pdf-form-filler","Fill in and flatten PDF forms.")
            environment={"HOME":str(root/"home"),"XDG_CONFIG_HOME":str(root/"config")}
            config = root / "config/capalith/settings.toml"
            config.parent.mkdir(parents=True,mode=0o700)
            with patch.dict(os.environ,environment,clear=True), patch("capalith.semantic.embed_documents",side_effect=lambda texts:tuple(vector for _ in texts)), patch("capalith.store._semantic_query_vector",return_value=vector):
                store = Store(root/"catalog.sqlite3")
                store.initialize()
                source = store.add_local_source(source_root)
                scan_local_source(store,source.id)
                old_key = semantic.model_key()
                self.assertIn("semantic",store.discover("pdf forms")["retrieval"]["method"])
                config.write_text("[semantic.projection]\nbody_chars = 128\n")
                config.chmod(0o600)
                stale = store.discover("pdf forms")
                self.assertEqual("stale",stale["semantic"]["status"])
                self.assertEqual("index_skills",stale["semantic"]["action"])
                scan_local_source(store,source.id)
                self.assertIn("semantic",store.discover("pdf forms")["retrieval"]["method"])
                connection = store._connect()
                try:
                    self.assertEqual(0,connection.execute("SELECT count(*) FROM catalog_semantic_vectors WHERE model_key=?",(old_key,)).fetchone()[0])
                finally:
                    connection.close()

    def test_public_package_contains_no_operator_environment_names(self):
        forbidden = ("aeth"+"er","kyd"+"ek","open"+"rig","orch"+"-lead","/"+"home"+"/")
        paths = [PROJECT_ROOT/"README.md", *list((PROJECT_ROOT/"capalith").rglob("*.py")), *list((PROJECT_ROOT/"capalith").rglob("*.json")), *list((PROJECT_ROOT/"docs").rglob("*.md")), *list((PROJECT_ROOT/"tests").rglob("*.py"))]
        for path in paths:
            text = path.read_text().casefold()
            for term in forbidden:
                self.assertFalse(term in text,str(path.relative_to(PROJECT_ROOT)) + " contains an operator-specific marker")


if __name__ == "__main__":
    unittest.main()
