# Capalith

Capalith is a local-first catalog for portable Agent Skills. It locates supported host skill sources, indexes their bundles, searches stored skill files, and follows declared relationships through MCP over stdio.

The full package installs three console commands and includes the semantic-search dependencies:

- `capalith` runs the JSON command-line interface.
- `capalith-mcp` starts without source or database arguments. The first `discover` call prepares the catalog and returns matches. `config_show` is an optional preview; `index_skills` explicitly refreshes the stored snapshot.
- `capalith-model provision` downloads and verifies the pinned local model. Run it once after package installation. `capalith-model verify` checks the installed model without network access.

The runtime and portable companion guide ship in the same package. The server sends the guide as MCP initialization instructions and exposes both guide files as MCP resources. No separate guide-copying step is required. Hosts retain control of tool exposure and skill loading.

Automatic discovery checks conventional Codex and Claude Code directories, the current Hermes profile and its configured external skill directories, and project skill locations. It does not scan the whole disk, other Hermes profiles, or host plugin registries, and does not prove host activation or usage. Repeat `--extra-source /absolute/folder` in the connection settings to add folders alongside native sources. Search also accepts `extra_source_paths` for connection-local additions, or `source_paths` for an explicit-only selection. Capalith creates or reuses its catalog under `$XDG_DATA_HOME/capalith/catalogs`, defaulting to `~/.local/share/capalith/catalogs`. Clients with the same selection and data directory share a catalog. Default-mode search can write this catalog on first use or context change; unchanged searches read the snapshot. Sources and host configuration are not changed.

Optional `--source ROOT` and `--git URL REF` connections index only those sources at startup and expose four read-only tools. Restart those modes to refresh. The default agent-managed connection has an additional `index_skills` tool and refreshes without restarting. No watcher or background service is installed.

For an existing manually managed catalog, `capalith-mcp --db PATH` preserves the read-only connection without setup or refresh. It cannot be combined with source-selection options. The JSON CLI still accepts `--db` for advanced catalog operations.

The portable skill remains at `capalith/skills/capalith` inside the installed Python package. Installing the package does not install or activate a native skill in an agent host.

After model provisioning, indexing and semantic search run locally. If the model becomes unavailable, SQLite BM25 handles the full query. This is a recovery path, not a reduced installation tier. The package has no optional feature extras.
