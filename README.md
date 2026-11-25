<p align="center">
  <img src="assets/capalith-mark.svg" alt="Capalith logo" width="240">
</p>

# Capalith

Capalith is a local-first catalog for portable agent skills. It indexes each skill with its supporting files from local directories or Git branches and tags. Search returns ranked matches, declared relationships, and the IDs a host needs to read a selected skill.

Capalith provides:

- local directories and Git branches or tags, with the resolved commit recorded for each scan;
- SQLite FTS5 search, optionally combined with a local semantic model;
- AI ranking for explicit searches with your own TypeSafe key or a host-managed transport, with abstention, local fallback and a reported answer path;
- stable pagination, inspection of stored files, and relationship traversal that handles cycles and enforces depth and result limits;
- sources that can be enabled, disabled, or removed without deleting history;
- read-only checks for changes to Git branch or tag sources;
- an MCP server over stdio with first-search catalog setup and stored-snapshot retrieval.

## Connect and use

Connect Capalith, then ask your agent to find a skill for a task. The server starts without a catalog or source arguments. The first search discovers supported skill directories, prepares the local catalog, and returns matches. No readiness call, database path, source IDs, separate scan command, or companion-guide installation is required. Capalith provides navigation; the agent decides when to use it and the host retains control of skill loading.

Supported: Linux on x86_64 with Python 3.11 and SQLite FTS5. Git is needed only for Git sources. Releases are distributed through GitHub, not a package registry. The latest release is [0.2.1](https://github.com/Kayhusk/capalith/releases/tag/v0.2.1), a public pre-release, and its README is at that tag; earlier releases are on the [releases page](https://github.com/Kayhusk/capalith/releases), and 0.1.4, 0.1.5 and 0.2.0 were never published (the [changelog](CHANGELOG.md) includes them in 0.2.1).

### Full installation

Linux on x86_64 with Python 3.11 (uv fetches it for you). Windows through WSL2 is expected to work as Linux but has not been tested yet; keep your skill folders and Capalith's files on the Linux side of WSL2. Plan for about 300 MB of disk: on a test machine the program and its libraries took 236 MB and the default search model about 67 MB.

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if you don't have it: `curl -LsSf https://astral.sh/uv/install.sh | sh` (it prints how to put uv on your PATH; open a new terminal afterwards, or run what it prints)
2. Install Capalith from its release wheel:

   ```bash
   uv tool install --python 3.11 https://github.com/Kayhusk/capalith/releases/download/v0.2.1/capalith-0.2.1-py3-none-any.whl
   ```

   To install a checkout of this repository instead (for example to try a change of your own), run `uv tool install --python 3.11 .` in it.

3. If uv warns that its tool folder is not on your PATH, run `uv tool update-shell` and open a new terminal ([uv's tool documentation](https://docs.astral.sh/uv/concepts/tools/)). If a command below says "command not found", this is why.
4. Download the search model once, then check it. `capalith-model provision` says what it will download before it starts (about 67 MB from Hugging Face) and where the files go (`$XDG_CACHE_HOME/capalith/models`, `~/.cache/capalith/models` by default); it needs the network once. Both commands print the model's path when they work.

   ```bash
   capalith-model provision
   capalith-model verify
   ```

On a test machine the install took about 40 seconds and the model download about 10.

To see which version a command runs, use `capalith --version`, `capalith-mcp --version` or `capalith-model --version`: each prints its own name and the installed version and exits 0. 0.1.3 does not have the flag; its three commands exit 2.

This installs the CLI, MCP server, bundled guidance, and semantic-search dependencies in an isolated environment. Provisioning downloads and verifies the pinned local model once. Indexing and the free search path then run locally. Enable AI ranking with your own TypeSafe key or a host-managed transport; otherwise the free ranking answers. See [ranking configuration and data flow](docs/ranking.md). The full install includes every shipped capability; there are no optional feature extras. SQLite BM25 remains the fallback if the local model is unavailable, not a reduced installation tier.

In an existing Python environment, `python3 -m pip install .` installs the same package. Do not install Capalith's dependencies into an agent host's own environment.

### Connect your agent

One command connects the two most common clients (each from its own documentation: [Claude Code, "Add a local stdio server"](https://code.claude.com/docs/en/mcp); [Codex, "MCP"](https://developers.openai.com/codex/mcp)). The `--` separates the client's own options from Capalith's command:

```bash
claude mcp add --transport stdio capalith -- capalith-mcp
codex mcp add capalith -- capalith-mcp
```

To search one more skill folder of your own, add `--extra-source /absolute/path` after `capalith-mcp` in the same command. Other clients take the same command in their own MCP settings, as below.

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

### Check that it works

Ask your agent: "Find a skill for tuning a violin." The agent searches in its own words; for the search `tuning a violin` the answer below is exactly what Capalith returned, on three made-up skills (violin tuning, pasta cooking, tide tables). The first search prepares the catalog and answers in about a second or two; later searches answer in a fraction of a second. The path after `read:` is the full path of that skill's `SKILL.md`; here it is shown with your home folder as a placeholder.

```
possible: Possible match. Read the first summary before you load it.
1. violin-tuning: Tune a violin by ear or with a tuner, string by string, and keep it in tune. (matched by meaning and your words 'tuning', 'violin')
   read: <your home folder>/.claude/skills/violin-tuning/SKILL.md
2. pasta-cooking: Cook dried pasta to al dente and pair it with a simple sauce. (matched by words only)
   read: <your home folder>/.claude/skills/pasta-cooking/SKILL.md
3. tide-tables: Read a harbour tide table and decide when a small boat can leave or return. (matched by words only)
   read: <your home folder>/.claude/skills/tide-tables/SKILL.md
```

The first skill is the one that fits; the other two are listed only because a search lists up to three skills by default. Read the first word and the first summary, not the whole list.

The first word says how far to trust the answer. A result marked `possible` or `weak` is a candidate to check, not a promise:

- `exact`: the skill's name is what you asked for.
- `good`: something judged the answer: AI ranking chose it, or you set `answer.good_from` (off by default; see [settings](docs/settings.md)).
- `possible`: a word of your task is in the skill's name or description, and nothing confirms it. On the free search this is the normal first answer: read the summary, then use the skill if it fits.
- `weak`: no word of yours is in the name or description; only the meaning, or only the skill's body, relates. Read the summary, and carry on without a skill if it does not fit.
- `close`: the first two results score nearly the same by meaning and no word of yours is in the first one's name or description. It does not say that either fits: read both summaries and pick one, or carry on without a skill.
- `no_match`: nothing matched; or you turned refusal on (see [settings](docs/settings.md)) and the best meaning score is below the number you set; or AI ranking is on and the AI says none of the skills it was shown fits (when it was shown only the nearest skills, the text says "None of the N nearest skills fits this task"). Carry on without a skill. With refusal off, the free search does not judge whether a non-empty list fits: it says `no_match` only when nothing matched.

If the first search answers `no_skill_sources`, Capalith found no skill folder (the message lists where it looked): put a skill (a folder with a `SKILL.md`) in a folder your agent reads, such as `~/.claude/skills`, or connect with `--extra-source` as above.

### Bring your own TypeSafe key

The built-in transport is included. Use your own TypeSafe account and key; Capalith supplies no key and never borrows another user's credentials.

1. Create a dedicated, revocable key for Capalith in the [TypeSafe console](https://console.typesafe.ai/keys).
2. Validate it before your first search with the free models endpoint. Run this in your own trusted Bash terminal, outside the agent's shell, with shell tracing off. The prompt hides input; the key is not exported or placed in curl's command-line arguments:

   ```bash
   read -rs -p 'Your dedicated TypeSafe API key: ' capalith_key
   printf '\n'
   printf 'Authorization: Bearer %s\n' "$capalith_key" |
     curl --disable --silent --show-error --fail --noproxy '*' \
       --header @- --output /dev/null --write-out 'HTTP %{http_code}\n' \
       https://api.typesafe.ai/v1/models
   unset capalith_key
   ```

   HTTP 200 confirms the key can list models; HTTP 401 means authentication failed. This check makes no paid inference request and does not prove a search works.
3. Set `CAPALITH_RANKING=typesafe` and `TYPESAFE_API_KEY` only in the MCP server's own secret or environment entry, then reconnect that server. Avoid exporting the key into an interactive shell shared with the agent. Run an explicit `discover` search and check `ranking.path` is `ai`; a `free` path includes `ranking.reason`. `missing_key` means TypeSafe was enabled but its key was absent or blank in the server environment; check that the client forwards it, then reconnect. Without the opt-in or a key, this route stays disabled; search is free unless a host-managed route is configured. Never put a key in a search, catalog file, ranking JSON or chat message.

A key in a shell or server environment can be read by the agent's own tools, and a malicious skill could try to obtain it. A server-specific secret entry limits where it is inherited; it does not isolate it from tools that can inspect that server. Use a dedicated key, watch its usage in the console, and revoke it to stop further spending.

The key is read from the worker's environment and sent only in the HTTPS authorization header to [TypeSafe's System One endpoint](https://docs.typesafe.ai/api/system-one), or to the `ranking.endpoint` you set in your own settings file (no query, project file or environment proxy can change it). The built-in transport follows no redirects, uses no environment proxy and retries no paid request. It sends your query, scoped skill names and descriptions, and shortlisted instruction excerpts. Source paths, catalog IDs and supporting resource files stay local. There are no automatic prompt hints or background provider calls.

An uncached successful search normally makes two requests. TypeSafe [currently charges $0.042 per million input tokens](https://docs.typesafe.ai/models), with output tokens free: 20,000 combined input tokens cost $0.00084. The [shipped policy](capalith/ranking-policy.json) caps each request at 240,000 bytes (240 KB). At roughly 60,000 input tokens, a near-limit request costs about $0.0025, or about $0.005 for a two-request search. Tokenization varies: the byte cap is not an exact token or dollar cap. There is no limit on how many searches an agent makes. Only identical queries with unchanged catalog, scope and configuration can reuse an unexpired cached view; different queries can each cost money. Cached continuation makes no provider request. A timeout or failed cache write can still leave a charge.

The built-in route has no spending cap or usage journal. [TypeSafe documents no per-key spend limit](https://help.typesafe.ai/articles/8310295754-who-can-create-and-manage-api-keys); its organization balance can still limit prepaid spending, subject to [auto-recharge and invoice billing](docs/ranking.md#bring-your-own-typesafe-key). Monitor Settings → Usage and revoke the dedicated key when needed. A valid-key search through the built-in route was not tested live before release; transport checks used a local stub.

Results report the AI or free path, and an AI abstention returns no candidates or recommendations. The private cache stores raw queries and accepted rankings with the policy's one-hour maximum age. Expired entries are never served and are securely deleted on the next access; an idle file persists until then. Unset `CAPALITH_RANKING` to disable the built-in route. The [ranking contract](docs/ranking.md) describes errors, retention and the host-managed alternative.

### Host-managed transport

An operator can keep its existing managed TypeSafe callable, importable by Capalith's Python interpreter. Put its `module:function` name and pointer-only options in a local JSON file, then set `CAPALITH_RANKING_CONFIG` to that file's absolute path in the CLI or MCP server environment. This explicit route takes precedence over the built-in opt-in. The file and containing directory must belong to the caller and have no group or world write permission, normally 0600 and 0700. Clients that filter environment variables must forward this setting and the transport's explicit absolute `PYTHONPATH` roots. See the [configuration example and transport contract](docs/ranking.md#host-managed-configuration).

Configured explicit searches send the query, scoped skill names and descriptions, and shortlisted instruction excerpts to the host's provider. Source paths, catalog IDs and supporting resource files stay local. The managed client owns credentials, permission, spending limits and attempted-call accounting; keys do not go in Capalith configuration. There are no automatic prompt hints or background provider calls.

The same ranking, fallback and continuation contract applies to both routes. The operator's managed client retains its credential and budget controls.

### Check readiness in the search runtime

Run `capalith-model verify` with the same environment that starts the server. A model provisioned under another profile's cache does not make this runtime ready. If verification fails because the pinned model is absent, use the documented `capalith-model provision` command in that environment, then verify again. Provisioning downloads model files; searching never downloads them. Keep the selected location within the current profile's permitted storage.

Check `retrieval.method` on a free search result: `sqlite_fts5_bm25_local_semantic_rrf` confirms that query used both local rankings. `sqlite_fts5_bm25` can mean the local model or indexed vectors were unavailable, or that no semantic candidate met the similarity threshold. Run the native model verification and check the selected catalog before attributing that result to setup. Stored vectors alone do not establish model readiness; a ready model does not guarantee a semantic match for every query. Do not lower the threshold just to change the reported method.

Measure model provisioning, first catalog preparation, the first query and repeated queries separately. A warm search does not include first-use setup, and a successful local setup does not establish ranking quality. Compare the same queries and expected skills before and after changing readiness or sources.

A server that has not searched for five minutes drops the search model from memory and loads it again, from the files already on disk, at its next search. `semantic.idle_release_seconds` sets the wait (`0` keeps the model); the first search after a release took about 0.14 to 0.16 s against about 0.04 s warm on our 174-skill catalog, and results are unchanged. What we measured, and its limits (one machine, a few runs, Linux only), is in [the settings page](docs/settings.md#releasing-the-search-model-when-idle).

### Select the host's actual skill roots

A fixed `--source` list searches only those roots. A catalog with no invalid or missing entries can still omit skills that the host exposes elsewhere, including a managed seat's projected skill directory. Catalog coverage counts describe the selected sources, not the host's active loadout.

When the host supplies exact authorized roots, pass them as `source_paths` to default-mode `discover` or `index_skills`, in precedence order. To retain selected shared roots, include those after the host root in the same explicit list. Capalith prepares a separate catalog for the selection; it does not require a copied skill tree or an edit to an existing catalog's registrations. Explicit `--source` connections instead require their owner to select the intended roots at startup; they reject per-search source changes.

Use the host's current inventory to check expected skill names against returned artifacts. Source discovery and a successful scan do not grant permission to load a skill. Do not infer managed locations from a seat name, scan other profiles, or treat stale catalog contents as a substitute for the host's current instructions.

### Automatic discovery and refresh

`discover` accepts a query directly and prepares the catalog on first use. It also accepts the actual absolute task `workspace`, including project switches. Without an explicit workspace, Capalith uses [Claude Code's `CLAUDE_PROJECT_DIR`](https://code.claude.com/docs/en/mcp#option-3-add-a-local-stdio-server) when supplied, then the server's working directory. Its installation directory, host profile, task workspace, and catalog can all live separately. `config_show` optionally previews readiness, source locations, and discovery context without creating a catalog.

When a client omits a custom host home from the MCP environment, the agent can supply the known `hermes`, `claude`, or `codex` config home through `host_homes` on the search itself. This overrides discovery context, not host configuration. `source_paths` accepts exact task-authorized roots or roots supplied by native host metadata instead of automatic location discovery. Users do not need to register those sources manually. `index_skills` remains available for an explicit refresh. Omitted context retains the selected workspace and host homes; switching projects does not require reconnecting.

The detector checks these locations, not the whole filesystem:

- [Codex](https://developers.openai.com/codex/skills): `~/.agents/skills`, plus the legacy `$CODEX_HOME/skills` location, defaulting to `~/.codex/skills`.
- [Claude Code](https://code.claude.com/docs/en/skills): `$CLAUDE_CONFIG_DIR/skills`, defaulting to `~/.claude/skills`.
- [Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills): the current `$HERMES_HOME/skills`, defaulting to `~/.hermes/skills`, plus `skills.create_dir` and `skills.external_dirs` from that profile's `config.yaml`. Configured paths expand `~` and environment variables; relative paths resolve against that profile home. Config reads are limited to 1 MiB and must be regular, non-symlinked files.
- Project `.agents/skills` and `.claude/skills` from the task directory through its nearest Git root, plus `.hermes/skills` at that root. Outside Git, only the task directory is checked.

Root symlinks and linked category or skill directories, including links nested inside categories, resolve to canonical intake roots. Traversal stops at each skill bundle and skips previously visited directories to avoid cycles. Supporting-file links inside bundles remain rejected. Identical canonical source roots are deduplicated. Discovery reports warnings and origins; it does not prove a host loaded or used the skills. Other Hermes profiles, plugin registries, managed enterprise locations, dynamic host state, and arbitrary workspace descendants are not searched. This is source-location discovery, not a replacement for each host's skill inventory, enablement, precedence, quarantine, or project-trust rules. The agent must honor those decisions before using retrieved guidance.

The catalog lives under `$XDG_DATA_HOME/capalith/catalogs`, or `~/.local/share/capalith/catalogs` by default. Its path depends on the selected sources and their order, so clients with the same selection and data directory reuse one catalog. First search and changed context prepare the catalog. Searches with unchanged context, including pagination, read stored snapshots without rescanning. Use `index_skills` to observe later source changes. There is no watcher or background service; the only timer drops the search model after it has been idle ([settings](docs/settings.md#releasing-the-search-model-when-idle)). Default-mode `discover` is annotated as catalog-writing and non-destructive; the other retrieval tools remain read-only.

After failed setup, correct the cause and use `index_skills`, or supply corrected context to `discover`. Unchanged searches do not retry setup or serve the prior catalog. Completed source scans remain stored; a failed source scan preserves that source's previous complete observation. Sources commit independently. Setup errors redact source and configuration details, with one exception: a catalog file written by a newer Capalith is refused with a fixed sentence that holds only two numbers and no path. Do not manually change registrations in an automatically managed catalog.

If you see `catalog_newer: this catalog was written by a newer Capalith (catalog format N; this version reads up to M). Upgrade Capalith, or point at another catalog.`, a newer Capalith has written this catalog (for example, a client that shares it was upgraded first). Upgrade this one, or select another catalog (another `--db`, another `storage.data_dir`, or a different source list); do not delete or edit the file. The same sentence comes from the MCP call, the server's startup message and the command line. 0.1.3 cannot say it: its MCP server answers `catalog_setup_failed` (or `database_unavailable` on a `--db` server) with no cause, and its command line prints only `unsupported store schema version: N`.

### Add your own folders

Keep native source discovery and add folders in the MCP connection's arguments:

```json
{
  "command": "capalith-mcp",
  "args": ["--extra-source", "/absolute/team-skills", "--extra-source", "/absolute/my-skills"]
}
```

These additions survive reconnects because the client owns the connection settings. Startup stays lazy. No new Capalith config file is needed. Native roots take precedence, followed by extras in the supplied order; canonical duplicates are merged. Missing or unreadable additions fail setup instead of silently dropping the folder.

For connection-local changes, `discover` and `index_skills` accept `extra_source_paths`. A supplied list replaces previous additions; `[]` clears them, and omission retains them. `source_paths` instead selects only explicit roots and cannot appear in the same call. Supplying workspace, host homes, or additions returns to automatic discovery unless explicit roots are also supplied. `--extra-source` cannot be combined with the explicit-only modes below or `--db`.

### Explicit startup sources

The installed server accepts local directories and Git branches or tags:

```bash
capalith-mcp --source /absolute/path/to/skills
capalith-mcp --git https://example.com/skills.git refs/heads/main
```

These optional modes index only the supplied sources at startup and then keep the catalog read-only. `discover` can maintain its separate ranking cache and is marked writing and non-destructive; the other query tools remain read-only. Repeat `--source` or `--git` to select more sources. Local sources take priority over Git sources; within each group, the first listed source wins a name collision.

Each server start refreshes this explicit selection. Git startup may fetch the configured ref; local startup needs no network. Restart these connections to pick up later source changes. If startup fails, the server exits rather than serving a stale catalog. Use `--db` below for manual source lifecycle control.

### Count your own usage (off by default)

To see how often your agents search, and how, turn on the local usage log in your settings file (`usage.enabled = true`). It writes one line per search on your own machine: the time, how many results, the status word, the duration and a random install id. It never writes the query, a skill's text, a path or an account, and nothing leaves your machine. `capalith usage` prints one line per day from it. Every setting, what a line holds and the measured cost are in [docs/settings.md](docs/settings.md#a-local-usage-log-off-by-default).

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

The semantic model uses the network only during `python3 -m capalith.semantic provision`; its scans and queries use local files with `local_files_only=True`. An enabled AI ranking transport separately sends explicit search inputs to its provider as described above.

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

By default an MCP `discover` call returns a short answer for an agent: a status, then each skill's name, one-sentence summary, reason and the absolute path of its `SKILL.md` (a Git-source skill gets an `inspect` call instead). Pass `detail: full`, or set `answer.detail = "full"` in your [settings](docs/settings.md), for the complete record. The CLI keeps the complete record. A full search returns ranked matches with their source, IDs, matching excerpts, declared relationships, recommendations, and pagination fields. The default page contains five matches. You can request up to 50 per page.

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

Traversal follows only declared `requires`, `complements`, `alternatives`, `conflicts`, and `supersedes` relationships. It is deterministic and cycle-safe; depth is bounded by `traversal.max_depth` (see [settings](docs/settings.md)).

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

Every mode exposes `discover`, `inspect`, `traverse`, and `config_show`. The default agent-managed connection also exposes `index_skills`. With `--db`, the server uses the exact database path supplied, does not create, migrate, or refresh the catalog, and does not read source files. `--db` cannot be combined with `--source`, `--git`, or `--extra-source`. Explicit-only connections reject automatic-discovery context arguments on search. No mode opens a network listener or configures hosts.

## Retrieval behavior

User-owned [settings](docs/settings.md) can override search, traversal, semantic-model and ranking defaults. No settings file is required.

Configured AI ranking compares the selected skills and verifies shortlisted instructions before accepting a match. The free path ranks BM25 and semantic matches separately, then combines those rankings. Exact names can still match below the semantic similarity threshold. If local semantic search cannot run safely, SQLite BM25 handles the free query.

Free results also include `semantic.status`, `reason`, `indexed` and `total`. `used` confirms a semantic contribution; `no_match` means no semantic candidate passed the gate; `unavailable` names a model or vector failure. `stale` asks for `index_skills` after an incomplete index or changed model/projection. A stale index falls back to lexical search until refreshed.

Only enabled sources are searched. Newer registrations win name collisions by default. Callers can pass an ordered list of source IDs to choose which sources to search and their priority. The agent decides when to search and the host controls skill loading and whether to enable AI ranking.

## Limits

Capalith does not install itself into an agent host. It catalogs, ranks and retrieves skills; it does not install, activate, execute, change, synchronize, sign, or publish them. The current release has no background service, user interface, general-purpose graph API or built-in host credential adapter. Git sources do not support embedded credentials, SSH, proxies, custom certificates, submodules, or LFS.

Dependency, model, and license records are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [DEPENDENCIES.json](DEPENDENCIES.json).

## License

Capalith is available under the [MIT License](LICENSE). Copyright 2026 Edward Bowie and Capalith contributors.
