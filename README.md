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
- an MCP server over stdio with agent-led indexing and read-only retrieval.

## Connect and use

Connect Capalith, then ask your agent to use it for a task. The server starts without a catalog or source arguments. Its bundled guidance and tool descriptions tell the agent how to check readiness, locate supported skill sources, index them, and retrieve relevant skill content. No database paths, source IDs, separate scan commands, or companion-guide installation are required.

The current package supports Linux with Python 3.11 or later and SQLite FTS5. Git is needed only for Git sources. Releases are distributed through GitHub, not a package registry. See the [changelog](CHANGELOG.md) and [release assets](https://github.com/Kayhusk/capalith/releases/tag/v0.1.2).

### Full installation

With [uv](https://docs.astral.sh/uv/guides/tools/) installed:

```bash
uv tool install --python 3.11 \
  https://github.com/Kayhusk/capalith/releases/download/v0.1.2/capalith-0.1.2-py3-none-any.whl
capalith-model provision
capalith-model verify
```

This installs the CLI, MCP server, bundled guidance, and semantic-search dependencies in an isolated environment. Provisioning downloads and verifies the pinned local model once. Search and indexing then run locally. The full install includes every shipped capability; there are no optional feature extras. SQLite BM25 remains the fallback if the model is unavailable, not a reduced installation tier.

For installation from a checkout, use `uv tool install --python 3.11 .`, followed by the same model commands. In an existing Python environment, `python3 -m pip install .` installs the same package. Do not install Capalith's dependencies into an agent host's own environment.

### Connect your agent

Clients that use `mcpServers` accept this form; other clients expose the same command through their native MCP settings:

```json
{
  "mcpServers": {
    "capalith": {
      "command": "capalith-mcp"
    }
  }
}
```

Use the absolute executable path returned by `command -v capalith-mcp` if the client does not inherit your shell's `PATH`. Capalith owns catalog setup. Your client owns connecting the server and exposing its tools. After connecting, ask the agent to find a skill for a task.

Hermes filters subprocess environment variables. Preserve its current profile in the same native connection operation:

```bash
PROFILE=default
hermes --profile "$PROFILE" mcp add capalith \
  --command "$(command -v capalith-mcp)" \
  --env "HERMES_HOME=$(dirname "$(hermes --profile "$PROFILE" config path)")"
```

Set `PROFILE` to the intended Hermes profile. The native config path resolves its actual directory, without relying on the shell's inherited profile environment. Use Hermes's `/reload-mcp` in the intended conversation after changing its connection. Clients that filter environment variables must also forward any custom `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, or variables used in external skill paths. Capalith sees the subprocess environment supplied by the client, not private parent-process state.

For profile-local model and catalog storage, set `CAPALITH_SEMANTIC_MODEL` during provisioning and in the MCP entry, and set the entry's `XDG_DATA_HOME` to that profile's chosen data directory. These settings change storage locations, not available features.

### Automatic discovery and refresh

`config_show` previews readiness, source locations, and discovery context without creating a catalog. On first use and after a project change, the agent passes its actual absolute `workspace` to `index_skills`. Without an explicit workspace, Capalith uses [Claude Code's `CLAUDE_PROJECT_DIR`](https://code.claude.com/docs/en/mcp#option-3-add-a-local-stdio-server) when supplied, then the server's working directory. Its installation directory, host profile, task workspace, and catalog can all live separately.

When a client omits a custom host home from the MCP environment, the agent can supply the known `hermes`, `claude`, or `codex` config home through `host_homes`. This overrides discovery context, not host configuration. `source_paths` accepts exact task-authorized roots or roots supplied by native host metadata instead of automatic location discovery. Users do not need to register those sources manually. No-argument refreshes retain the selected workspace and host homes; switching projects does not require reconnecting.

The detector checks these locations, not the whole filesystem:

- [Codex](https://developers.openai.com/codex/skills): `~/.agents/skills`, plus the legacy `$CODEX_HOME/skills` location, defaulting to `~/.codex/skills`.
- [Claude Code](https://code.claude.com/docs/en/skills): `$CLAUDE_CONFIG_DIR/skills`, defaulting to `~/.claude/skills`.
- [Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills): the current `$HERMES_HOME/skills`, defaulting to `~/.hermes/skills`, plus `skills.create_dir` and `skills.external_dirs` from that profile's `config.yaml`. Configured paths expand `~` and environment variables; relative paths resolve against that profile home. Config reads are limited to 1 MiB and must be regular, non-symlinked files.
- Project `.agents/skills` and `.claude/skills` from the task directory through its nearest Git root, plus `.hermes/skills` at that root. Outside Git, only the task directory is checked.

Root symlinks and linked category or skill directories, including links nested inside categories, resolve to canonical intake roots. Traversal stops at each skill bundle and skips previously visited directories to avoid cycles. Supporting-file links inside bundles remain rejected. Identical canonical source roots are deduplicated. Discovery reports warnings and origins; it does not prove a host loaded or used the skills. Other Hermes profiles, plugin registries, managed enterprise locations, dynamic host state, and arbitrary workspace descendants are not searched. This is source-location discovery, not a replacement for each host's skill inventory, enablement, precedence, quarantine, or project-trust rules. The agent must honor those decisions before using retrieved guidance.

The catalog lives under `$XDG_DATA_HOME/capalith/catalogs`, or `~/.local/share/capalith/catalogs` by default. Its path depends on the selected sources and their order, so clients with the same selection and data directory reuse one catalog. Retrieval reads stored snapshots. There is no watcher or background service.

After a failed indexing call, the connection requires successful indexing before retrieval resumes. Completed source scans remain stored; a failed source scan preserves that source's previous complete observation. Sources commit independently. Setup errors redact source and configuration details. Do not manually change registrations in an automatically managed catalog.

### Explicit startup sources

The installed server accepts local directories and Git branches or tags:

```bash
capalith-mcp --source /absolute/path/to/skills
capalith-mcp --git https://example.com/skills.git refs/heads/main
```

These optional modes index only the supplied sources at startup and expose read-only query tools. Repeat `--source` or `--git` to select more sources. Local sources take priority over Git sources; within each group, the first listed source wins a name collision.

Each server start refreshes this explicit selection. Git startup may fetch the configured ref; local startup needs no network. Restart these connections to pick up later source changes. If startup fails, the server exits rather than serving a stale catalog. Use `--db` below for manual source lifecycle control.

### Bundled guidance

The server supplies the companion skill as MCP initialization instructions. It also exposes the exact bundled files at `capalith://guide/SKILL.md` and `capalith://guide/references/operations.md`. Clients decide how to expose those instructions and resources. Capalith does not install or activate a native host skill, and no separate skill installation is required by the server.

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

## Advanced: manage a catalog through the CLI

These commands are optional. Use them when you want direct control of database placement, source lifecycle, or scan timing instead of source-selected MCP startup.

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

Inspection reads the copy stored in SQLite. It does not reopen the source directory. A resource-specific read returns identity, catalog status, and that resource without manifests, version history, other resources, or relationships. Omit the resource path for full audit inspection. Prefer the host's native reader for ordinary skill use, and reuse instructions already loaded instead of fetching a second copy.

Traversal follows only declared `requires`, `complements`, `alternatives`, `conflicts`, and `supersedes` relationships. It is deterministic, cycle-safe, and limited to depths 1 through 3.

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

## Connect an existing catalog read-only

```bash
python3 -m capalith.mcp_server --db "$DB"
```

Every mode exposes `discover`, `inspect`, `traverse`, and `config_show`. The default agent-managed connection also exposes `index_skills`. With `--db`, the server uses the exact database path supplied, does not create, migrate, or refresh the catalog, and does not read source files. `--db` cannot be combined with `--source` or `--git`. No mode opens a network listener or configures hosts.

## Retrieval behavior

Capalith ranks BM25 and semantic matches separately, then combines the rankings. Exact names can still match below the semantic similarity threshold. If semantic search cannot run safely, SQLite BM25 handles the full query.

Only enabled sources are searched. Newer registrations win name collisions by default. Callers can pass an ordered list of source IDs to choose which sources to search and their priority. Capalith does not decide when an agent should search, load selected content through a host, or run an LLM.

## Limits

Capalith does not install itself into an agent host. It only catalogs and retrieves skills. It does not install, activate, execute, change, synchronize, sign, or publish them. The current release has no background service, user interface, general-purpose graph API, model reranker, or host adapter. Git sources do not support embedded credentials, SSH, proxies, custom certificates, submodules, or LFS.

Dependency, model, and license records are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [DEPENDENCIES.json](DEPENDENCIES.json).

## License

Capalith is available under the [MIT License](LICENSE). Copyright 2026 Edward Bowie and Capalith contributors.
