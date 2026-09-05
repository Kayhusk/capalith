from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import anyio
import mcp_types as types
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError
from mcp.types import TextContent
from mcp_types import METHOD_NOT_FOUND, PARSE_ERROR

import capalith
from capalith.cli import _configuration_report
from capalith.identity import artifact_id, hash_bundle
from capalith.intake import scan_local_source
from capalith.mcp_server import create_server
from capalith.store import Store
from tests.helpers import make_skill


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parameters(database: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "capalith.mcp_server", "--db", str(database)],
        cwd=PROJECT_ROOT,
    )


class MCPServerTests(unittest.TestCase):
    def test_server_reports_package_version(self) -> None:
        server = create_server(Path("unused.sqlite3"))
        self.assertEqual(capalith.__version__, server.version)

    def test_stdio_server_lists_and_calls_the_four_read_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source_root = temp / "source"
            bundle = make_skill(source_root, "mcp-demo", b"mcp evidence")
            database = temp / "capalith.sqlite3"
            store = Store(database)
            store.initialize()
            source = store.add_local_source(source_root)
            scan_local_source(store, source.id)
            identifier = artifact_id(source.id, "mcp-demo")
            digest = hash_bundle(bundle).digest
            source_before = hash_bundle(bundle)
            database_before = database.read_bytes()

            async def exercise() -> None:
                async with stdio_client(parameters(database)) as streams:
                    async with ClientSession(*streams) as session:
                        initialized = await session.initialize()
                        self.assertFalse(
                            initialized.capabilities.model_dump(by_alias=True)["tools"][
                                "listChanged"
                            ]
                        )
                        listed = await session.list_tools()
                        self.assertEqual(
                            ["discover", "inspect", "traverse", "config_show"],
                            [tool.name for tool in listed.tools],
                        )
                        tools = {tool.name: tool for tool in listed.tools}
                        for tool in listed.tools:
                            self.assertFalse(tool.input_schema["additionalProperties"])
                            self.assertEqual({"type": "object"}, tool.output_schema)
                            assert tool.annotations is not None
                            self.assertEqual(
                                {"readOnlyHint": True, "openWorldHint": False},
                                tool.annotations.model_dump(by_alias=True, exclude_none=True),
                            )
                        self.assertEqual(["query"], tools["discover"].input_schema["required"])
                        self.assertEqual(50, tools["discover"].input_schema["properties"]["limit"]["maximum"])
                        self.assertEqual(
                            ["artifact_id", "content_digest"],
                            tools["inspect"].input_schema["required"],
                        )
                        self.assertEqual(3, tools["traverse"].input_schema["properties"]["depth"]["maximum"])
                        self.assertEqual({}, tools["config_show"].input_schema["properties"])

                        requests = (
                            (
                                "discover",
                                {"query": "mcp", "source_ids": [source.id], "limit": 1},
                                store.discover("mcp", source_ids=(source.id,), limit=1),
                            ),
                            (
                                "inspect",
                                {"artifact_id": identifier, "content_digest": digest},
                                store.get_artifact(identifier, digest),
                            ),
                            (
                                "traverse",
                                {
                                    "artifact_id": identifier,
                                    "content_digest": digest,
                                    "source_ids": [source.id],
                                    "relationship_types": ["requires"],
                                    "depth": 2,
                                },
                                store.traverse(
                                    identifier,
                                    digest,
                                    source_ids=(source.id,),
                                    relationship_types=("requires",),
                                    depth=2,
                                ),
                            ),
                            ("config_show", {}, _configuration_report(database)),
                        )
                        for name, arguments, expected in requests:
                            result = await session.call_tool(name, arguments)
                            self.assertFalse(result.is_error)
                            self.assertEqual(expected, result.structured_content)
                            self.assertEqual(1, len(result.content))
                            content = result.content[0]
                            self.assertIsInstance(content, TextContent)
                            assert isinstance(content, TextContent)
                            self.assertEqual(
                                json.dumps(expected, sort_keys=True, separators=(",", ":")),
                                content.text,
                            )

            anyio.run(exercise)
            self.assertEqual(source_before, hash_bundle(bundle))
            self.assertEqual(database_before, database.read_bytes())

    def test_stdio_server_maps_protocol_and_application_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source_root = temp / "source"
            bundle = make_skill(source_root, "mcp-errors", b"mcp errors")
            database = temp / "capalith.sqlite3"
            store = Store(database)
            store.initialize()
            source = store.add_local_source(source_root)
            scan_local_source(store, source.id)
            identifier = artifact_id(source.id, "mcp-errors")
            digest = hash_bundle(bundle).digest

            async def assert_error(
                session: ClientSession,
                name: str,
                arguments: dict[str, Any],
                code: str,
            ) -> None:
                result = await session.call_tool(name, arguments)
                self.assertTrue(result.is_error)
                self.assertIsNone(result.structured_content)
                content = result.content[0]
                self.assertIsInstance(content, TextContent)
                assert isinstance(content, TextContent)
                self.assertEqual(code, content.text)

            async def exercise() -> None:
                async with stdio_client(parameters(database)) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        cases = (
                            ("discover", {"query": "mcp", "unexpected": True}, "invalid_request"),
                            (
                                "inspect",
                                {"artifact_id": identifier, "content_digest": "sha256:" + "0" * 64},
                                "stale_artifact",
                            ),
                            (
                                "inspect",
                                {"artifact_id": "missing", "content_digest": digest},
                                "artifact_not_found",
                            ),
                            (
                                "inspect",
                                {
                                    "artifact_id": identifier,
                                    "content_digest": digest,
                                    "resource_path": "../outside",
                                },
                                "invalid_request",
                            ),
                            (
                                "inspect",
                                {
                                    "artifact_id": identifier,
                                    "content_digest": digest,
                                    "resource_path": "references/missing.md",
                                },
                                "resource_not_found",
                            ),
                            (
                                "discover",
                                {"query": "mcp", "source_ids": ["missing"]},
                                "source_unavailable",
                            ),
                        )
                        for name, arguments, code in cases:
                            await assert_error(session, name, arguments, code)

                        first = await session.call_tool("discover", {"query": "mcp"})
                        assert first.structured_content is not None
                        Store(database).set_source_status(source.id, "disabled")
                        await assert_error(
                            session,
                            "discover",
                            {
                                "query": "mcp",
                                "view_id": first.structured_content["retrieval"]["view_id"],
                            },
                            "stale_view",
                        )
                        with self.assertRaises(MCPError) as raised:
                            await session.call_tool("unknown", {})
                        self.assertEqual(METHOD_NOT_FOUND, raised.exception.code)

                missing = temp / "missing.sqlite3"
                async with stdio_client(parameters(missing)) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        for name, arguments in (
                            ("discover", {"query": "mcp"}),
                            ("inspect", {"artifact_id": identifier, "content_digest": digest}),
                            ("traverse", {"artifact_id": identifier, "content_digest": digest}),
                        ):
                            await assert_error(session, name, arguments, "database_unavailable")
                        report = await session.call_tool("config_show", {})
                        self.assertFalse(report.is_error)
                        assert report.structured_content is not None
                        self.assertEqual([], report.structured_content["sources"])
                self.assertFalse(missing.exists())

            anyio.run(exercise)

    def test_malformed_envelope_uses_a_protocol_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "missing.sqlite3"
            messages = (
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            wire = b"".join(
                json.dumps(message, separators=(",", ":")).encode() + b"\n"
                for message in messages
            ) + b"{broken\n"
            result = subprocess.run(
                [sys.executable, "-m", "capalith.mcp_server", "--db", str(database)],
                cwd=PROJECT_ROOT,
                input=wire,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
            )
            self.assertEqual(0, result.returncode)
            lines = result.stdout.splitlines()
            self.assertEqual(2, len(lines))
            initialized, malformed = (json.loads(line) for line in lines)
            self.assertEqual(1, initialized["id"])
            self.assertIn("result", initialized)
            self.assertEqual(("2.0", None, PARSE_ERROR), (
                malformed["jsonrpc"], malformed["id"], malformed["error"]["code"]
            ))

    def test_unexpected_handler_failure_is_redacted(self) -> None:
        server = create_server(Path("secret-database.sqlite3"))
        entry = server.get_request_handler("tools/call")
        assert entry is not None
        handler = cast(Any, entry.handler)

        async def exercise() -> None:
            with patch(
                "capalith.mcp_server._store_call",
                side_effect=RuntimeError("secret content and /private/path"),
            ):
                result = await handler(
                    None,
                    types.CallToolRequestParams(name="config_show", arguments={}),
                )
            self.assertTrue(result.is_error)
            self.assertIsNone(result.structured_content)
            content = result.content[0]
            assert isinstance(content, TextContent)
            self.assertEqual("internal_error", content.text)

        anyio.run(exercise)


if __name__ == "__main__":
    unittest.main()
