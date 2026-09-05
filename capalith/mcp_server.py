from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

import anyio
import mcp_types as types
from anyio.to_thread import run_sync
from jsonschema import Draft202012Validator
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from capalith import __version__
from capalith.catalog import CatalogError
from capalith.cli import _configuration_report
from capalith.store import Store, StoreError

_DIGEST_PATTERN = "^sha256:[0-9a-f]{64}$"
_RELATIONSHIP_TYPES = [
    "requires",
    "complements",
    "alternatives",
    "conflicts",
    "supersedes",
]
_READ_ONLY = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
_OUTPUT_SCHEMA = {"type": "object"}
_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "discover": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Natural-language task to search for.",
            },
            "source_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
                "description": "Sources to search, in priority order.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 5,
                "description": "Maximum results per page.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Number of results to skip.",
            },
            "view_id": {
                "anyOf": [
                    {"type": "string", "pattern": "^d1:[0-9a-f]{64}$"},
                    {"type": "null"},
                ],
                "default": None,
                "description": "View ID returned by an earlier page of the same search.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "inspect": {
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "minLength": 1,
                "description": "Artifact ID returned by Capalith.",
            },
            "content_digest": {
                "type": "string",
                "pattern": _DIGEST_PATTERN,
                "description": "Content digest paired with the artifact ID.",
            },
            "resource_path": {
                "anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
                "description": "Stored relative path to read; omit it to list resources.",
            },
        },
        "required": ["artifact_id", "content_digest"],
        "additionalProperties": False,
    },
    "traverse": {
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "minLength": 1,
                "description": "Starting artifact ID returned by Capalith.",
            },
            "content_digest": {
                "type": "string",
                "pattern": _DIGEST_PATTERN,
                "description": "Content digest paired with the starting artifact ID.",
            },
            "source_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
                "description": "Enabled sources to include, in priority order.",
            },
            "relationship_types": {
                "type": "array",
                "items": {"enum": _RELATIONSHIP_TYPES},
                "uniqueItems": True,
                "description": "Relationship types to follow.",
            },
            "direction": {
                "enum": ["outbound", "inbound", "both"],
                "default": "outbound",
                "description": "Direction to follow relationships.",
            },
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "default": 1,
                "description": "Maximum relationship depth.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 10,
                "description": "Maximum results per page.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Number of results to skip.",
            },
            "view_id": {
                "anyOf": [
                    {"type": "string", "pattern": "^t1:[0-9a-f]{64}$"},
                    {"type": "null"},
                ],
                "default": None,
                "description": "View ID returned by an earlier page of the same traversal.",
            },
        },
        "required": ["artifact_id", "content_digest"],
        "additionalProperties": False,
    },
    "config_show": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}
_TOOLS = [
    types.Tool(
        name=name,
        description=description,
        input_schema=_INPUT_SCHEMAS[name],
        output_schema=_OUTPUT_SCHEMA,
        annotations=_READ_ONLY,
    )
    for name, description in (
        ("discover", "Search the latest scanned Agent Skills for a task description."),
        ("inspect", "Return stored metadata, a resource list, or one resource for an artifact version."),
        ("traverse", "Return declared relationships for an artifact version."),
        ("config_show", "Return configured sources, source states, and Capalith features."),
    )
]
_VALIDATORS = {
    name: Draft202012Validator(schema) for name, schema in _INPUT_SCHEMAS.items()
}
_CATALOG_CODES = {
    "artifact_not_found",
    "invalid_request",
    "resource_not_found",
    "source_unavailable",
    "stale_artifact",
    "stale_view",
}


def _success(value: dict[str, object]) -> types.CallToolResult:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=value,
    )


def _failure(code: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=code)],
        is_error=True,
    )


def _store_call(database: Path, name: str, arguments: dict[str, Any]) -> dict[str, object]:
    if name == "config_show":
        return _configuration_report(database)

    store = Store(database, read_only=True)
    values = dict(arguments)
    if "source_ids" in values:
        values["source_ids"] = tuple(values["source_ids"])
    if "relationship_types" in values:
        values["relationship_types"] = tuple(values["relationship_types"])
    if name == "discover":
        return store.discover(**values)
    if name == "inspect":
        return store.get_artifact(**values)
    return store.traverse(**values)


def create_server(database: Path) -> Server[Any]:
    async def list_tools(
        _context: object, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_TOOLS)

    async def call_tool(
        _context: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        name = params.name
        if name not in _VALIDATORS:
            raise MCPError(types.METHOD_NOT_FOUND, "Unknown tool")
        try:
            arguments = params.arguments or {}
            errors = list(_VALIDATORS[name].iter_errors(arguments))
            if errors:
                return _failure("invalid_request")
            value = await run_sync(lambda: _store_call(database, name, arguments))
            return _success(value)
        except StoreError as error:
            code = str(error).partition(":")[0]
            return _failure(code if code in _CATALOG_CODES else "internal_error")
        except CatalogError:
            return _failure("invalid_request")
        except (OSError, sqlite3.Error):
            return _failure(
                "database_unavailable"
                if name in ("discover", "inspect", "traverse")
                else "internal_error"
            )
        except Exception:
            return _failure("internal_error")

    server = Server(
        "capalith",
        version=__version__,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    server.middleware.clear()
    return server


async def _serve(database: Path) -> None:
    server = create_server(database)
    async with stdio_server() as (read_stream, write_stream):
        incoming_send, incoming_receive = anyio.create_memory_object_stream[Any](0)

        async def forward() -> None:
            async with read_stream, incoming_send:
                async for message in read_stream:
                    if isinstance(message, Exception):
                        code = types.INVALID_REQUEST
                        text = "Invalid Request"
                        if isinstance(message, ValidationError) and any(
                            error["type"] == "json_invalid"
                            for error in message.errors()
                        ):
                            code = types.PARSE_ERROR
                            text = "Parse error"
                        await write_stream.send(
                            SessionMessage(
                                types.JSONRPCError(
                                    jsonrpc="2.0",
                                    id=None,
                                    error=types.ErrorData(code=code, message=text),
                                )
                            )
                        )
                    else:
                        await incoming_send.send(message)

        async with incoming_receive, anyio.create_task_group() as tasks:
            tasks.start_soon(forward)
            await server.run(
                incoming_receive,
                write_stream,
                server.create_initialization_options(),
                raise_exceptions=False,
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="capalith-mcp",
        description="Run Capalith's read-only MCP server over stdio.",
    )
    parser.add_argument("--db", type=Path, required=True, metavar="PATH", help="catalog database path")
    arguments = parser.parse_args(argv)
    try:
        anyio.run(_serve, Path(os.path.abspath(arguments.db)))
    except Exception:
        print("internal_error", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
