<p align="center">
  <img src="assets/capalith-mark.svg" alt="Capalith logo" width="240">
</p>

# Capalith

Capalith is a local-first catalog for portable agent skills. It indexes each skill with its supporting files from local directories or Git branches and tags. Search returns ranked matches, declared relationships, and the IDs a host needs to read a selected skill.

Capalith provides:

- local directories and Git branches or tags, with the resolved commit recorded for each scan;
- SQLite FTS5 search, optionally combined with a local semantic model;
- stable pagination, inspection of stored files, and relationship traversal that handles cycles and enforces depth and result limits;
- sources that can be enabled, disabled, or removed without deleting history;
- read-only checks for changes to Git branch or tag sources;
- a read-only MCP server over stdio.

## Install from this repository

No package has been published to a registry. Install the checkout directly:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
```

SQLite BM25 works with the default install. Use `python3 -m pip install '.[semantic]'` only when local semantic search is required.

## Development setup

`requirements.txt` pins all dependencies used by the test suite, including MCP and `fastembed`, for CPython 3.11 on Linux x86_64. SQLite must include FTS5.

```bash
python3 -m pip install --require-hashes -r requirements.txt
PYTHONWARNINGS=error python3 -m unittest discover -s tests -v
```

These tests cover package and MCP protocol behavior. They do not verify integration with a specific host, remote access, deployment, or package publication.

The semantic model is local. It converts skill text and queries into vectors so related wording can match when BM25 does not. `fastembed` runs the pinned ONNX model on the CPU.

Download and verify the model once:

```bash
python3 -m capalith.semantic provision
python3 -m capalith.semantic verify
```

Only `python3 -m capalith.semantic provision` uses the network. Scans and searches use local files with `local_files_only=True`.

## Add and search a local source

```bash
DB="${DB:-capalith.sqlite3}"
ROOT="/path/to/a/skills/root"

SOURCE_ID="$(
  python3 -m capalith --db "$DB" source add-local "$ROOT" |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["source_id"])'
)"

python3 -m capalith --db "$DB" scan "$SOURCE_ID"
python3 -m capalith --db "$DB" --source-id "$SOURCE_ID" discover "your query"
```

Search returns ranked matches with their source, IDs, matching excerpts, declared relationships, recommendations, and pagination fields. The default page contains five matches. You can request up to 50 per page.

To request the next page without changing the result set, reuse its `view_id` and `next_offset`:

```bash
python3 -m capalith --db "$DB" --source-id "$SOURCE_ID" --limit 10 \
  --offset "$NEXT_OFFSET" --view-id "$VIEW_ID" discover "your query"
```

A view becomes stale if the query, selected sources, catalog, retrieval method, model, or ranking rules change.

## Inspect and traverse

Use the artifact ID and content digest returned by discovery:

```bash
python3 -m capalith --db "$DB" artifact show "$ARTIFACT_ID" "$CONTENT_DIGEST"
python3 -m capalith --db "$DB" artifact show \
  "$ARTIFACT_ID" "$CONTENT_DIGEST" "references/guide.md"

python3 -m capalith --db "$DB" --source-id "$SOURCE_ID" \
  --relationship-type requires --direction both --depth 2 --limit 10 \
  traverse "$ARTIFACT_ID" "$CONTENT_DIGEST"
```

Inspection reads the copy stored in SQLite. It does not reopen the source directory. Traversal follows only declared `requires`, `complements`, `alternatives`, `conflicts`, and `supersedes` relationships. It is deterministic, cycle-safe, and limited to depths 1 through 3.

## Add a Git branch or tag source

Capalith accepts credential-free `https://` URLs without query data and local `file://` URLs. The requested ref must be a fully qualified branch or tag.

```bash
SOURCE_ID="$(
  python3 -m capalith --db "$DB" source add-git \
    "$GIT_URL" "refs/heads/main" |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["source_id"])'
)"
python3 -m capalith --db "$DB" scan "$SOURCE_ID"
python3 -m capalith --db "$DB" source review "$SOURCE_ID"
```

Each scan records the requested ref and resolved commit. Later scans of a moving ref keep records of earlier revisions.

Review fetches the configured ref, compares its commit and skill contents with the last successful scan, and reports any changes without updating the catalog. After upgrading an older database that stores multiple Git revisions for one source, scan that source once before review.

## Enable, disable, or remove sources

```bash
python3 -m capalith --db "$DB" source list
python3 -m capalith --db "$DB" source disable "$SOURCE_ID"
python3 -m capalith --db "$DB" source enable "$SOURCE_ID"
python3 -m capalith --db "$DB" source remove "$SOURCE_ID"
python3 -m capalith --db "$DB" config show
```

Disabled sources remain in the database and can be enabled without another scan. Removed sources remain in history but cannot be enabled again. `config show` is read-only and works before the database exists.

## MCP server

```bash
python3 -m capalith.mcp_server --db "$DB"
```

The server exposes `discover`, `inspect`, `traverse`, and `config_show` as structured MCP tools. It uses the database path passed at startup, does not create or migrate the database, and does not read source files or open a network listener. Capalith does not configure or modify hosts.

## Retrieval behavior

Capalith ranks BM25 and semantic matches separately, then combines the rankings. Exact names can still match below the semantic similarity threshold. If semantic search cannot run safely, SQLite BM25 handles the full query.

Only enabled sources are searched. Newer registrations win name collisions by default. Callers can pass an ordered list of source IDs to choose which sources to search and their priority. Capalith does not decide when an agent should search, load selected content through a host, or run an LLM.

## Limits

Capalith does not install itself into an agent host. It only catalogs and retrieves skills. It does not install, activate, execute, change, synchronize, sign, or publish them. The current release has no background service, user interface, general-purpose graph API, model reranker, or host adapter. Git sources do not support embedded credentials, SSH, proxies, custom certificates, submodules, or LFS.

Dependency, model, and license records are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [DEPENDENCIES.json](DEPENDENCIES.json).

## License

Capalith is available under the [MIT License](LICENSE). Copyright 2026 Edward Bowie and Capalith contributors.
