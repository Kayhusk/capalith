from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence, TextIO

from capalith.catalog import CatalogError
from capalith.identity import BundleEntryError
from capalith.intake import review_git_source, scan_git_source, scan_local_source
from capalith.store import Source, Store, StoreError


def _source_value(source: Source) -> dict[str, object]:
    value = asdict(source)
    result = {
        "kind": value["kind"],
        "locator": value["locator"],
        "source_id": value["id"],
        "status": value["status"],
    }
    if source.kind == "git":
        result["requested_ref"] = value["requested_ref"]
    return result


def _existing_store(database: Path) -> Store:
    if not database.is_file():
        raise FileNotFoundError(f"database does not exist: {database}")
    store = Store(database)
    store.initialize()
    return store


def _add_local(database: Path, locator: str) -> dict[str, object]:
    store = Store(database)
    root = store.validate_local_source(Path(locator))
    database.parent.mkdir(parents=True, exist_ok=True)
    store.initialize()
    return _source_value(store.add_local_source(root))


def _add_git(
    database: Path, locator: str, requested_ref: str
) -> dict[str, object]:
    store = Store(database)
    store.validate_git_source(locator, requested_ref)
    database.parent.mkdir(parents=True, exist_ok=True)
    store.initialize()
    return _source_value(store.add_git_source(locator, requested_ref))


def _set_path(database: Path, source_id: str, locator: str) -> dict[str, object]:
    root = Store(database).validate_local_source(Path(locator))
    store = _existing_store(database)
    return _source_value(store.set_local_source_path(source_id, root))


def _list_sources(database: Path) -> list[dict[str, object]]:
    return [_source_value(source) for source in _existing_store(database).list_sources()]


def _configuration_report(database: Path) -> dict[str, object]:
    sources = (
        [
            _source_value(source)
            for source in Store(database, read_only=True).list_sources()
        ]
        if database.is_file()
        else []
    )
    return {
        "schema_version": 1,
        "sources": sources,
        "automatic_behavior": {
            "automatic_refresh": "unavailable",
            "discovery": "operator_invoked",
            "source_scan": "operator_invoked",
        },
        "host_integrations": {
            "installed": [],
            "native_delegation": "unavailable",
        },
        "capabilities": {
            "artifact_inspection": "available",
            "automatic_refresh": "unavailable",
            "background_synchronization": "unavailable",
            "configuration_report": "available",
            "discovery": "available",
            "discovery_continuation": "available",
            "host_integration": "unavailable",
            "native_delegation": "unavailable",
            "relationship_traversal": "available",
            "source_currentness": "available",
            "source_intake": "available",
        },
        "authority_boundary": {
            "capalith": ["read_configured_sources", "write_owned_database"],
            "host": [
                "activation",
                "configuration",
                "learning",
                "lifecycle",
                "loading",
            ],
        },
    }


def _set_source_status(
    database: Path, source_id: str, status: str
) -> dict[str, object]:
    return _source_value(_existing_store(database).set_source_status(source_id, status))


def _scan(database: Path, source_id: str) -> dict[str, object]:
    store = _existing_store(database)
    source = store.get_source(source_id, require_enabled=True)
    if source.kind == "local":
        return asdict(scan_local_source(store, source_id))
    result, revision = scan_git_source(store, source_id)
    return {**asdict(result), **asdict(revision)}


def _review(database: Path, source_id: str) -> dict[str, object]:
    if not database.is_file():
        raise FileNotFoundError("database does not exist")
    return asdict(review_git_source(Store(database, read_only=True), source_id))


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--source-id", action="append", dest="source_ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--view-id")
    parser.add_argument(
        "--relationship-type",
        action="append",
        dest="relationship_types",
        choices=("requires", "complements", "alternatives", "conflicts", "supersedes"),
    )
    parser.add_argument(
        "--direction", choices=("outbound", "inbound", "both"), default="outbound"
    )
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument(
        "command",
        choices=("source", "scan", "artifact", "discover", "traverse", "config"),
    )
    parser.add_argument("arguments", nargs="*")
    arguments = parser.parse_args(argv)

    try:
        try:
            database = Path(arguments.db).resolve(strict=False)
        except RuntimeError as error:
            raise StoreError(f"could not resolve database path: {arguments.db}") from error
        command_arguments = arguments.arguments
        if arguments.command == "config" and command_arguments == ["show"]:
            value = _configuration_report(database)
        elif arguments.command == "source" and command_arguments == ["list"]:
            value = _list_sources(database)
        elif arguments.command == "source" and len(command_arguments) == 2:
            action, locator = command_arguments
            if action == "review":
                value = _review(database, locator)
            elif action == "add-local":
                value = _add_local(database, locator)
            elif action in ("enable", "disable", "remove"):
                status = {
                    "enable": "enabled",
                    "disable": "disabled",
                    "remove": "removed",
                }[action]
                value = _set_source_status(database, locator, status)
            else:
                parser.error(
                    "source requires 'list', 'review SOURCE_ID', 'add-local ROOT', "
                    "'add-git URL REF', 'set-path SOURCE_ID ROOT', or "
                    "'enable|disable|remove SOURCE_ID'"
                )
        elif arguments.command == "source" and len(command_arguments) == 3:
            action, first, second = command_arguments
            if action == "add-git":
                value = _add_git(database, first, second)
            elif action == "set-path":
                value = _set_path(database, first, second)
            else:
                parser.error(
                    "source requires 'list', 'review SOURCE_ID', 'add-local ROOT', "
                    "'add-git URL REF', 'set-path SOURCE_ID ROOT', or "
                    "'enable|disable|remove SOURCE_ID'"
                )
        elif arguments.command == "scan" and len(command_arguments) == 1:
            value = _scan(database, command_arguments[0])
        elif arguments.command == "artifact" and command_arguments == ["list"]:
            value = _existing_store(database).list_artifacts()
        elif arguments.command == "discover":
            if not command_arguments:
                raise CatalogError("discovery query must contain a word")
            value = _existing_store(database).discover(
                " ".join(command_arguments),
                source_ids=(
                    tuple(arguments.source_ids)
                    if arguments.source_ids is not None
                    else None
                ),
                limit=arguments.limit if arguments.limit is not None else 5,
                offset=arguments.offset,
                view_id=arguments.view_id,
            )
        elif arguments.command == "traverse" and len(command_arguments) == 2:
            value = _existing_store(database).traverse(
                command_arguments[0],
                command_arguments[1],
                source_ids=(
                    tuple(arguments.source_ids)
                    if arguments.source_ids is not None
                    else None
                ),
                relationship_types=(
                    tuple(arguments.relationship_types)
                    if arguments.relationship_types is not None
                    else None
                ),
                direction=arguments.direction,
                depth=arguments.depth,
                limit=arguments.limit if arguments.limit is not None else 10,
                offset=arguments.offset,
                view_id=arguments.view_id,
            )
        elif (
            arguments.command == "artifact"
            and len(command_arguments) in (3, 4)
            and command_arguments[0] == "show"
        ):
            value = _existing_store(database).get_artifact(
                command_arguments[1],
                command_arguments[2],
                command_arguments[3] if len(command_arguments) == 4 else None,
            )
        else:
            parser.error("invalid command arguments")
    except sqlite3.Error:
        print("database operation failed", file=stderr)
        return 1
    except (BundleEntryError, CatalogError, StoreError, OSError) as error:
        print("\\n".join(str(error).splitlines()), file=stderr)
        return 1

    stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    return 0
