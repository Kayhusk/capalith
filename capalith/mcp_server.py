from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from importlib.resources import files
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
from capalith.onboarding import discover_sources, prepare_catalog
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
_INDEX_TOOL = types.Tool(
    name="index_skills",
    description=(
        "Find and index installed Codex, Claude Code and current Hermes profile skill directories. "
        "On first use or project change, supply the actual task workspace before discover. "
        "Supply host_homes from known host context if the client's environment omitted a custom home. "
        "No-argument calls refresh the selection; source_paths selects only explicit custom roots. "
        "Creates Capalith's catalog, never edits sources or activates skills. config_show previews roots."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "source_paths": {
                "type": "array", "minItems": 1,
                "items": {"type": "string", "minLength": 1},
                "description": "Absolute skill directories selected by the user or task.",
            },
            "workspace": {"type": "string", "minLength": 1,
                          "description": "Absolute task workspace; falls back to CLAUDE_PROJECT_DIR, then server cwd."},
            "host_homes": {
                "type": "object",
                "properties": {host: {"type": "string", "minLength": 1}
                               for host in ("hermes", "claude", "codex")},
                "additionalProperties": False,
                "description": "Known absolute host config homes, not installation or skill paths. "
                               "Overrides filtered environment values; omitted keys use the server environment.",
            },
        },
        "additionalProperties": False,
    },
    output_schema=_OUTPUT_SCHEMA,
    annotations=types.ToolAnnotations(
        read_only_hint=False, destructive_hint=False, open_world_hint=False,
    ),
)
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


def create_server(database: Path | None = None, *, managed: bool = False) -> Server[Any]:
    agent_managed = database is None
    catalog_lock = anyio.Lock()
    selected_paths: list[Path] | None = None
    selected_workspace: Path | None = None
    selected_homes: dict[str, Path] = {}
    tools = [*_TOOLS, _INDEX_TOOL] if agent_managed else _TOOLS
    validators = {tool.name: Draft202012Validator(tool.input_schema) for tool in tools}
    bundle = files("capalith").joinpath("skills/capalith")
    guides = {
        f"capalith://guide/{path}": bundle.joinpath(path).read_text(encoding="utf-8")
        for path in ("SKILL.md", "references/operations.md")
    }

    async def list_resources(
        _context: object, _params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        return types.ListResourcesResult(resources=[
            types.Resource(uri=uri, name=uri.removeprefix("capalith://guide/"), mime_type="text/markdown")
            for uri in guides
        ])

    async def read_resource(
        _context: object, params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        if params.uri not in guides:
            raise MCPError(types.INVALID_PARAMS, "Unknown guide resource")
        return types.ReadResourceResult(contents=[
            types.TextResourceContents(uri=params.uri, mime_type="text/markdown", text=guides[params.uri])
        ])

    async def list_tools(
        _context: object, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def call_tool(
        _context: object, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        nonlocal database, selected_paths, selected_workspace, selected_homes
        name = params.name
        if name not in validators:
            raise MCPError(types.METHOD_NOT_FOUND, "Unknown tool")
        try:
            arguments = params.arguments or {}
            errors = list(validators[name].iter_errors(arguments))
            if errors:
                return _failure("invalid_request")
            async with catalog_lock:
                if name == "index_skills":
                    workspace = Path(arguments["workspace"]) if "workspace" in arguments else selected_workspace
                    homes = ({host: Path(p) for host, p in arguments["host_homes"].items()}
                             if "host_homes" in arguments else selected_homes)
                    roots = None if "workspace" in arguments or "host_homes" in arguments else selected_paths
                    if "source_paths" in arguments:
                        roots = [Path(p) for p in arguments["source_paths"]]
                    if (workspace is not None and not workspace.is_absolute()) or (
                        roots is not None and any(not root.is_absolute() for root in roots)
                    ) or any(not home.is_absolute() for home in homes.values()):
                        return _failure("invalid_request")
                    selected_workspace, selected_paths, selected_homes = workspace, roots, homes
                    database = None
                    try:
                        if roots is None:
                            discovery = await run_sync(lambda: discover_sources(selected_workspace, selected_homes))
                            discovered = discovery["sources"]
                            assert isinstance(discovered, list)
                            roots = [Path(s["path"]) for s in discovered]
                        if not roots:
                            return _failure("no_skill_sources")
                        database = await run_sync(lambda: prepare_catalog(roots))
                    except Exception:
                        return _failure("catalog_setup_failed")
                    name = "config_show"
                if agent_managed and name == "config_show":
                    value = await run_sync(lambda: _configuration_report(database))
                    artifacts = await run_sync(Store(database, read_only=True).list_artifacts) if database else []
                    value["catalog"] = {
                        "mode": "agent_managed", "state": "ready" if database else "not_indexed",
                        "database_path": str(database) if database else None,
                        "present_artifacts": sum(bool(a["present"]) for a in artifacts),
                    }
                    value["source_discovery"] = (
                        {"sources": [{"path": str(p), "origins": ["explicit"]} for p in selected_paths],
                         "warnings": []} if selected_paths is not None else
                        await run_sync(lambda: discover_sources(selected_workspace, selected_homes))
                    )
                    value["automatic_behavior"] = {
                        "source_discovery": "documented_host_locations",
                        "source_scan": "agent_invoked", "automatic_refresh": "agent_invoked",
                    }
                    capabilities = value["capabilities"]
                    assert isinstance(capabilities, dict)
                    capabilities["agent_indexing"] = "available"
                    return _success(value)
                active_database = database
                if active_database is None:
                    return _failure("index_required")
                value = await run_sync(lambda: _store_call(active_database, name, arguments))
            if name == "config_show" and managed:
                value["catalog"] = {"database_path": str(database), "mode": "managed"}
                value["automatic_behavior"] = {
                    "automatic_refresh": "server_startup",
                    "discovery": "operator_invoked",
                    "source_scan": "server_startup",
                }
                capabilities = value["capabilities"]
                assert isinstance(capabilities, dict)
                capabilities["automatic_refresh"] = "server_startup"
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
        instructions=guides["capalith://guide/SKILL.md"].split("---", 2)[-1].strip().replace(
            "(references/operations.md)", "(capalith://guide/references/operations.md)"
        ),
        on_list_resources=list_resources,
        on_read_resource=read_resource,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    server.middleware.clear()
    return server


async def _serve(database: Path | None, managed: bool = False) -> None:
    server = create_server(database, managed=managed)
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
        description="Agent-led skill discovery and indexing over stdio; --db keeps tools read-only.",
    )
    parser.add_argument("--db", type=Path, metavar="PATH", help="existing database; no setup or refresh")
    parser.add_argument(
        "--source", type=Path, action="append", metavar="ROOT",
        help="skill directory to index at startup; repeat in priority order",
    )
    parser.add_argument(
        "--git", nargs=2, action="append", metavar=("URL", "REF"),
        help="Git branch or tag to index at startup; repeat in priority order after local sources",
    )
    arguments = parser.parse_args(argv)
    managed = bool(arguments.source or arguments.git)
    if arguments.db and managed:
        parser.error("--db cannot be combined with --source/--git")
    try:
        if managed:
            try:
                database = prepare_catalog(arguments.source or (), arguments.git or ())
            except Exception:
                print(
                    "catalog_setup_failed: check selected source paths, Git URL/ref, "
                    "and writable XDG_DATA_HOME outside the sources",
                    file=sys.stderr,
                )
                return 1
        elif arguments.db:
            database = Path(os.path.abspath(arguments.db))
        else:
            database = None
        anyio.run(_serve, database, managed)
    except Exception:
        print("internal_error", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
