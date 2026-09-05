# Capalith

Capalith is a local-first catalog for portable agent skills. It inventories complete bundles from local directories and exact Git revisions, returns a compact ranked shortlist, records declared relationships and provenance, and returns identifiers that a host can pass to its native reader or loader.

This checkout provides:

- local-directory and Git branch or tag intake with resolved commit provenance;
- SQLite FTS5 retrieval with optional local semantic fusion;
- deterministic pagination, inspection, and bounded relationship traversal;
- source enable, disable, and remove states without deleting history;
- read-only drift review for Git branch or tag sources;
- a read-only MCP stdio server for `discover`, `inspect`, `traverse`, and `config_show`.

BM25 is the complete fallback when the local semantic model or valid vector coverage is unavailable.

## Install from this repository

No package has been published to a registry. Install the checkout directly:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
```

SQLite BM25 works with the default install. Use `python3 -m pip install '.[semantic]'` only when local semantic retrieval is required.

## Development setup

The admitted dependency lock installs the complete checkout runtime, including MCP and `fastembed`, for CPython 3.11 on Linux x86_64 with SQLite FTS5.

```bash
python3 -m pip install --require-hashes -r requirements.txt
PYTHONWARNINGS=error python3 -m unittest discover -s tests -v
```

The local semantic model is an embedding model, not a chat model or hosted service. It converts skill text and queries into numerical representations so retrieval can match related wording that BM25 may miss. `fastembed` runs the pinned ONNX artifact on the local CPU.

Provision and verify that artifact once:

```bash
python3 -m capalith.semantic provision
python3 -m capalith.semantic verify
```

Provisioning is the only semantic operation that uses the network. Scans and queries run against local files with `local_files_only=True`.

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

Discovery returns compact candidates with source, bundle, artifact, digest, ranking, evidence, relationship, recommendation, and continuation data. The default page contains five candidates. A call may request up to 50.

Continue a frozen result view with its `view_id` and `next_offset`:

```bash
python3 -m capalith --db "$DB" --source-id "$SOURCE_ID" --limit 10 \
  --offset "$NEXT_OFFSET" --view-id "$VIEW_ID" discover "your query"
```

A changed query, source scope, index state, retrieval method, model identity, or ranking constant makes the old view stale.

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

Inspection reads stored bundle bytes from one SQLite snapshot. It does not reopen the source tree. Traversal follows only declared `requires`, `complements`, `alternatives`, `conflicts`, and `supersedes` relationships. It is deterministic, cycle-safe, and bounded to depth 1 through 3.

## Add a Git branch or tag source

Capalith admits `https://` URLs without credentials or query data and local `file://` URLs. The requested ref must be a fully qualified branch or tag.

```bash
SOURCE_ID="$(
  python3 -m capalith --db "$DB" source add-git \
    "$GIT_URL" "refs/heads/main" |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["source_id"])'
)"
python3 -m capalith --db "$DB" scan "$SOURCE_ID"
python3 -m capalith --db "$DB" source review "$SOURCE_ID"
```

Each scan records the requested ref and resolved commit. Later scans of a moving ref preserve prior revision provenance.

Review stages the configured ref, compares the fetched commit and bundle digests with the last successful scan, and returns evidence only. It does not scan or update the catalog. On a migrated database, a source with more than one recorded Git revision needs one successful scan before review can establish the current revision.

## Control source participation

```bash
python3 -m capalith --db "$DB" source list
python3 -m capalith --db "$DB" source disable "$SOURCE_ID"
python3 -m capalith --db "$DB" source enable "$SOURCE_ID"
python3 -m capalith --db "$DB" source remove "$SOURCE_ID"
python3 -m capalith --db "$DB" config show
```

Disabled sources keep their catalog and can be enabled without a rescan. Removed sources retain history but cannot return to the active configuration. `config show` is read-only and works before the database exists.

## MCP server

```bash
python3 -m capalith.mcp_server --db "$DB"
```

The server exposes the same four read operations as the Python and CLI interfaces. It fixes the database path at startup, uses structured MCP results, does not create or migrate the database, and does not read source files or open a network listener. Capalith does not configure or modify hosts.

## Retrieval behavior

Capalith ranks lexical and local semantic results independently and fuses their ranks. Exact names remain eligible even below the semantic similarity floor. If semantic retrieval cannot run safely, SQLite BM25 handles the full query.

Only enabled sources participate. By default, newer registrations take precedence when names collide. Callers may pass ordered source IDs to select and prioritize an exact source scope. Capalith does not infer agent search policy, load content into a host, or add another LLM to the retrieval path.

## Development checks

Run the warning-strict suite for repository changes:

```bash
PYTHONWARNINGS=error python3 -m unittest discover -s tests -v
```

The repository suite and generic MCP tests do not establish named-host support, remote transport, deployment, or publication.

## Limits

The package is not published and does not install itself into an agent host. Capalith does not install, activate, validate, repair, synchronize, archive, sign, publish, or govern skills. It has no watcher, worker, network service, user interface, generic graph query API, model reranker, or host adapter. Git intake excludes private credentials, SSH, proxies, custom certificates, submodules, and LFS.

Dependency, model, license, and artifact records are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [DEPENDENCIES.json](DEPENDENCIES.json).

## License

Capalith is available under the [MIT License](LICENSE). Copyright 2026 Edward Bowie and Capalith contributors.
