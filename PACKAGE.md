# Capalith

Capalith is a local-first catalog for portable Agent Skills. It locates supported host skill sources, indexes their bundles, searches stored skill files, and follows declared relationships through MCP over stdio.

The full package installs three console commands and includes the semantic-search dependencies:

- `capalith` runs the JSON command-line interface.
- `capalith-mcp` starts without source or database arguments. The first `discover` call prepares the catalog and returns matches. `config_show` is an optional preview; `index_skills` explicitly refreshes the stored snapshot.
- `capalith-model provision` downloads and verifies the pinned local model. Run it once after package installation. `capalith-model verify` checks the installed model without network access.

The runtime and portable companion guide ship in the same package. The server sends the guide as MCP initialization instructions and exposes both guide files as MCP resources. No separate guide-copying step is required. Hosts retain control of tool exposure and skill loading.

Automatic discovery checks conventional Codex and Claude Code directories, the current Hermes profile and its configured external skill directories, and project skill locations. It does not scan the whole disk, other Hermes profiles, or host plugin registries, and does not prove host activation or usage. Repeat `--extra-source /absolute/folder` in the connection settings to add folders alongside native sources. Search also accepts `extra_source_paths` for connection-local additions, or `source_paths` for an explicit-only selection. Capalith creates or reuses its catalog under `$XDG_DATA_HOME/capalith/catalogs`, defaulting to `~/.local/share/capalith/catalogs`. Clients with the same selection and data directory share a catalog. Default-mode search can write this catalog on first use or context change; unchanged searches read the snapshot. Sources and host configuration are not changed.

Optional `--source ROOT` and `--git URL REF` connections index only those sources at startup and expose four query tools. The catalog is then read-only; `discover` can maintain its separate ranking cache and is marked writing and non-destructive. Restart those modes to refresh. The default agent-managed connection has an additional `index_skills` tool and refreshes without restarting. No watcher or background service is installed.

For an existing manually managed catalog, `capalith-mcp --db PATH` preserves the read-only catalog connection without setup or refresh; discovery's ranking cache is separate. It cannot be combined with source-selection options. The JSON CLI still accepts `--db` for advanced catalog operations.

The portable skill remains at `capalith/skills/capalith` inside the installed Python package. Installing the package does not install or activate a native skill in an agent host.

After model provisioning, indexing and the free semantic search path run locally. AI ranking is available for explicit searches with your own TypeSafe API key or a host-managed transport. It supports abstention and local fallback for missing configuration, errors or timeout. Results state which path answered. If the local model becomes unavailable, SQLite BM25 handles the free query. The package has no optional feature extras.

## Bring your own TypeSafe key

1. Create a dedicated, revocable key for Capalith in the [TypeSafe console](https://console.typesafe.ai/keys). Capalith includes no key and never uses another person's account. Before your first search, use the [free curl key check](https://github.com/Kayhusk/capalith/blob/main/README.md#bring-your-own-typesafe-key) against `https://api.typesafe.ai/v1/models`: HTTP 200 confirms model-list access, while HTTP 401 means authentication failed. It makes no paid inference request.
2. Set `CAPALITH_RANKING=typesafe` and `TYPESAFE_API_KEY` only in the MCP server's own secret or environment entry. The built-in transport is included; no Python adapter or ranking JSON file is needed. Never paste a key into chat, search arguments or a catalog, or export it into an interactive shell shared with the agent.
3. Run an explicit `discover` search and check `ranking.path` is `ai`. A `free` result includes a fallback reason. Without a key or the opt-in, the built-in route stays disabled; search is free unless a host-managed route is configured. Clients that filter environments must forward both variables; reconnect the server after changing its environment.

The key goes only in the HTTPS authorization header to [TypeSafe's System One API](https://docs.typesafe.ai/api/system-one), never to results, errors, logs or the cache. The built-in transport does not follow redirects, use environment proxies or retry paid requests. Unset `CAPALITH_RANKING` to disable it.

A key in a shell or server environment can be read by the agent's own tools; a malicious skill could try. A server-specific entry limits inheritance but cannot prevent tools with access to that server from reading it. Use a dedicated key, monitor its usage in the console, and revoke it to stop further spending.

An uncached successful AI search normally makes two requests. At [TypeSafe's published $0.042 per million input tokens](https://docs.typesafe.ai/models), 20,000 combined input tokens cost $0.00084, with output tokens free. Each request is capped at 240,000 bytes (240 KB): roughly 60,000 input tokens would cost about $0.0025 per request and $0.005 per two-request search. Tokenization varies, so this byte cap is not an exact token or dollar cap. There is no limit on the number of searches an agent makes. Only identical queries on an unchanged catalog, scope and configuration reuse an unexpired cached view; different queries can each cost money. Cached continuation makes no provider request; a timeout or failed cache write can still leave a charge.

The built-in route has no spending cap or usage journal, and [TypeSafe documents no per-key spend limit](https://help.typesafe.ai/articles/8310295754-who-can-create-and-manage-api-keys). Its organization balance can still limit prepaid spending, subject to [auto-recharge and invoice billing](https://github.com/Kayhusk/capalith/blob/main/docs/ranking.md#bring-your-own-typesafe-key). Check Settings → Usage and revoke the dedicated key when needed. A valid-key search through the built-in route was not tested live before release; transport checks used a local stub.

## Host-managed option

The host operator can retain an existing trusted Python `module:function` callable and pointer-only options in a local JSON file, then set its absolute path as `CAPALITH_RANKING_CONFIG` in the CLI or MCP server environment. This explicit route takes precedence over the built-in opt-in. The same interpreter must be able to import that callable; clients that filter environments must forward the setting and explicit absolute transport module roots. The configuration file and its containing directory must belong to the caller and must not be group- or world-writable, normally 0600 and 0700. The managed client owns credentials, permission, budgets and usage accounting. See [the ranking contract](https://github.com/Kayhusk/capalith/blob/main/docs/ranking.md).

## Search data and retention

AI search sends skill names as well as descriptions and shortlisted instruction excerpts; source paths, catalog IDs and supporting resource files stay local. No background provider calls or automatic prompt hints are installed. Results identify the `ai` or `free` path and fallback reason; an AI abstention returns no matches or recommendations. The private view cache includes raw queries, expires views after the policy's one-hour age limit, and securely deletes expired rows on the next access. An idle cache file persists until then. A failed cache write after a paid response can cause a later new search to pay again; no automatic paid retry occurs.

<!-- Prepared for the MCP registry, not registered: the registry would start `uvx capalith`, the JSON command-line tool, not the server. -->
<!-- mcp-name: io.github.Kayhusk/capalith -->
