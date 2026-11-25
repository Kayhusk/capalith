# User settings

Capalith works without a settings file. Its defaults live in [settings-defaults.json](../capalith/settings-defaults.json), with optional AI ranking defaults in [ranking-policy.json](../capalith/ranking-policy.json).

For an override, create `$XDG_CONFIG_HOME/capalith/settings.toml`, or `~/.config/capalith/settings.toml` when `XDG_CONFIG_HOME` is unset. Use an absolute `CAPALITH_CONFIG` path to select another file; `capalith --config PATH` and `capalith-mcp --config PATH` select that same file. The file and its containing directory must belong to the current user and have no group or world write permission. The file must be regular, have one hard link, and be at most 64 KiB. Symlink traversal is refused. Capalith reads this file and never writes it.

A command that finds the settings folder or file open to your group or to others answers `settings_untrusted: directory: <folder> can be written by group or others. Fix: chmod go-w <folder>` (for the file, `chmod go-w <file>`). Run the `chmod` it names (the path in the command is shell-quoted, so a name with spaces or quotes can be pasted as it is); some systems create new folders group-writable. Control characters in a path are shown as code points in every message, and a path that holds any gets no command at all: the message says to rename it and repeat. A settings folder that exists is checked even when it holds no file. A link, a file with another owner or more than one hard link, or an unreadable path gets a message that says why and names no `chmod`.

```toml
version = 1

[search]
default_limit = 4

[search.weights]
path = 3.0
body = 1.0

[semantic.projection]
body_chars = 128
```

Only supplied keys replace defaults; lists replace the whole list. Unknown keys, wrong types and invalid core values produce `settings_error`; unsafe files produce `settings_untrusted`. Errors do not echo values. The CLI exits 2 for invalid core settings; MCP tools return an error rather than searching with a partial configuration. An explicitly selected missing file is an error; an absent default file uses packaged defaults.

Project files, skill contents and tool-call arguments cannot set configuration. Existing call arguments such as `limit` and source selection keep their documented meaning. A configuration snapshot lasts for one search, traversal or indexing operation, so an edit during indexing applies to the next operation.

## Search and traversal

| Table | Keys | Meaning |
| --- | --- | --- |
| `search` | `default_limit`, `max_limit` | Positive page sizes; the default cannot exceed the maximum. An explicit call limit overrides the default. |
| `search` | `lexical_candidates`, `rrf_k`, `excerpt_chars` | `lexical_candidates` is kept for compatibility and has no effect: the word search reads every match once (the value must still be a positive number). `rrf_k` is the reciprocal-rank constant and `excerpt_chars` the excerpt length; both must be positive. |
| `search` | `minimum_similarity` | Semantic gate from 0 to 1; exact skill names retain their bypass. This is not an overall no-answer threshold. |
| `search` | `stopwords`, `operator` | A list of individual words to omit from queries, and `OR` or `AND` between the remaining words. |
| `search.weights` | `path`, `body` | Nonnegative BM25 field weights. Names and descriptions remain in the existing indexed body; they have no separate field weights. |
| `search.fusion_weights` | `lexical`, `semantic` | Nonnegative reciprocal-rank contribution weights; at least one must be positive. |
| `search` | `cached_catalogs`, `cache_max_bytes` | Bounds on retained prepared catalogs and their estimated Python/vector bytes. Set either to zero to disable retention. This does not limit total process RSS or SQLite's own temporary storage. |
| `traversal` | `default_limit`, `max_limit`, `max_depth` | Positive default/maximum page sizes and maximum graph depth. |

Warm search reuses catalog lookups and a local vector matrix. Every search still securely opens and checks the catalog identity. A replaced file, another connection's commit, a source-scope change or a relevant configuration change invalidates preparation. The cache keeps no read transaction between calls; the next query observes a committed refresh. It does not rescan source files or create a background worker; the only timer is the one that drops the search model when it has been idle (see [Releasing the search model when idle](#releasing-the-search-model-when-idle)). A new process or evicted catalog pays preparation cost again.

## Semantic model and index identity

`semantic.enabled` controls local semantic search. `threads` and `batch_size` must be positive. `semantic.projection.body_chars` chooses how much body text joins the name and description; `projection.version` is a positive projection revision.

`semantic.download_bytes` is the size `capalith-model provision` shows before it downloads (a whole number of bytes: about 67 MB for the default model; a profile sets it, and 0 shows "an unknown size"). `semantic.model` accepts `id`, `artifact`, `revision`, positive `dimensions`, an absolute `dir` (or empty for the standard cache), and a `files` table mapping model-relative paths to SHA-256 hashes. Choose a model supported by the installed embedding library and provide its complete matching file/hash inventory. Hashes remain mandatory. Changing a model does not download it during search: use `capalith-model provision`, then `capalith-model verify` in the server's environment.

Model content and projection settings determine the vector fingerprint. Moving the same files to another directory does not change it. Exact shipped defaults retain compatibility with existing indexes. Internal `compatibility` metadata is not a user setting. Search never mixes vectors from different fingerprints. An incomplete or mismatched index reports `semantic.status = "stale"` and `action = "index_skills"`, while lexical retrieval continues. Refresh with `index_skills`, or scan registered sources in CLI mode. Default-mode first preparation may index automatically; an explicit `--db` MCP connection stays read-only and must be refreshed by its owner.

After successful embedding, `storage.keep_old_vectors = false` removes old model vectors for refreshed artifacts. Set it to true to retain them. Source files and artifact history are unaffected. An unavailable local model leaves lexical search available and reports its fallback.

## Releasing the search model when idle

`semantic.idle_release_seconds` is how long a server waits after its last use of the search model before it drops the model from memory. The default is `300` (five minutes); `0` keeps the model for as long as the process runs. Any number from 0 to 86,400 is valid, whole or with a fraction; anything else is a `settings_error`. Once the model is dropped, the memory it held is handed back to the operating system where the platform's C library allows it (glibc, so Linux; elsewhere the model is still dropped and the system may get back less). The next search loads the model again from the files already on disk (nothing is downloaded) and answers as before: the vectors are the same, and a release never takes the model from a call that is using it. The setting is not part of the index fingerprint, so changing it re-indexes nothing; a change takes effect with the next search.

What we measured, in plain words. One machine, Linux, real MCP servers, the setting at 3 seconds; memory is proportional set size (PSS) in MiB, summed over the servers. On a 174-skill catalog, eight runs with one server (on two builds of the idle release) went from 238 to 246 MiB before the release to 129 to 161 MiB after it, and one run with five servers from 1,038 to 539 MiB. The first search after a release took about 0.14 to 0.16 s in those eight runs, against about 0.04 s for a warm search. On a catalog of 100,125 skills (mostly synthetic filler), one run with one server went from 844 to 729 MiB and one with five servers from 4,087 to 3,512 MiB, and the first search after the release took 0.30 s. The saving varies from run to run; what is given back is the model's share of a server, so a large catalog saves a smaller part of the total. Not measured: macOS, more than five servers, a long-running real server, and a host under other load.

## Choosing the embedding model

`semantic.profile` selects a packaged model: `bge-small-en-v1.5` (the default, the model in the packaged defaults) or `arctic-embed-s` (`snowflake/snowflake-arctic-embed-s`, Apache-2.0, 130 MB, 384 dimensions, pinned by revision and file hashes in `capalith/model-profiles.json`). A profile replaces the model identity and the semantic gate (`search.minimum_similarity`, which is 0 for `arctic-embed-s`: the 0.66 gate belongs to bge-small); your own `semantic.model` and `search` keys still win. Run `capalith-model provision` after choosing it; files are checked against the pinned hashes like the default model's. Changing the model changes the index fingerprint: searches report `semantic: stale` and fall back to words until `index_skills` re-embeds the catalog.

**Make the search say "no skill fits".** By default the free search only says what it knows: it reports `possible` when a word of your task is in a skill's name or description, `weak` when less than that matches, and `no_match` only when nothing matched at all (with AI ranking on, `no_match` also comes when the AI says none of the skills it was shown fits). It does not say that no skill fits a task, because that needs a threshold on the meaning score, and the threshold depends on the model. To turn refusal on, use the optional model `arctic-embed-s` (set `profile = "arctic-embed-s"` under `[semantic]`, then run `capalith-model provision`; about 130 MB) and set `refuse_below = 0.768` under `[answer]`.

What we measured, in plain words, as totals. On our test tasks (87 tasks we wrote for one 174-skill catalog; development data, not a promise for your skills) this setting refused 21 of the 31 tasks that have no skill and wrongly refused 2 of the 56 tasks that do. It refused 13 of 15 far-off tasks (tasks nowhere near any skill) but only 4 of 12 near misses (tasks that sound close to a skill but have none). On a small separate set of tasks we had not looked at while tuning it, the same rule refused 3 of 7 tasks with no skill and wrongly refused 2 of 28 others. That set is small; read it as a check, not a measurement. So expect a clear "no" for far-off tasks and often a `possible` or `weak` answer for near misses.

The number 0.768 belongs to this model and to that catalog's skill text. With your own skills, try your own tasks before you rely on it. We do not recommend refusing with the default model (`bge-small-en-v1.5`): on our test tasks it refused 12 of 31, and only 1 of 12 near misses. Refusal is off by default.

## The agent's answer

| Table | Keys | Meaning |
| --- | --- | --- |
| `answer` | `detail` | `short` (default) or `full`. MCP `discover` returns the short answer; `full` returns the complete record, as before. A call's `detail` argument overrides it. The CLI always returns the complete record. |
| `answer` | `default_limit` | How many skills the short answer lists when the call gives no `limit` (default 3, at least 1). The smaller of this and `search.default_limit` applies; a call's `limit` and the full record keep their own page size. |
| `answer` | `summary_chars`, `max_chars` | The longest summary per skill (at least 20) and the character budget for the whole text (0 turns it off). Over the budget the text gives up, in order: summary length (to 50 characters), summaries, the `instead` and `related` lines and redirect extras, then long paths (said as "path too long", never cut), then the end of the list ("N more in the structured result"). The structured record always holds every skill and the full paths. |
| `answer` | `max_matched_words`, `hide_words` | How many matched words to show per skill, and common words never shown. |
| `answer` | `close_margin` | With no query word in the first result's name or description, two results are `close` when their semantic scores differ, either way, by no more than this (0 to 1). |
| `answer` | `redirects`, `max_redirects` | A skill whose description says "for X use Y" lists Y after the page when X shares a word with the task; at most `max_redirects` such skills. |
| `answer` | `refuse_below` | A number from 0 to 1, default `0.0` (off). When above 0, a free search whose best meaning score over all searched skills is below it says `no_match`, with the nearest skills named as near misses. The value belongs to the embedding model: see `semantic.profile`. An exact name match is never refused. |
| `answer` | `good_from` | A number from 0 to 1, default `0.0` (off). When above 0, a first result that has a query word in its name or description and a meaning score at or above this reads `good`. On our test tasks, 0.70 called 15 answers good: 13 were the right skill and 2 were wrong. The value belongs to the embedding model: see `semantic.profile`. |
| `answer.messages` | `exact`, `good`, `possible`, `close`, `weak`, `no_match`, `no_match_nearest` | The sentence after each status. Only `{name}`, `{nearest}`, `{signal}` (the reason the rule gave), `{sent}` and `{total}` may appear in them. `no_match_nearest` replaces `no_match` when AI ranking was shown only the nearest skills (`ranking.roster.sent` is below `total`): "None of the {sent} nearest skills fits this task. Carry on without one." |

The status comes from one rule, `judge` in `capalith/answer.py`, over the signals the search already has. `exact`: the name equals the query. `good`: something judged the answer: the AI ranking chose it, or `answer.good_from` is set, the first result's meaning score is at or above it, and a query word is in its name or description. `possible`: a query word is in the first result's name or description and nothing confirms it; with the shipped settings on the free search this is the usual first answer. `weak`: no query word is in the name or description, and only the meaning, or only the instructions text, relates; the message says which. `close`: the same case, and the first two semantic scores are within `close_margin`. `no_match`: nothing matched at all (this includes an AI ranking that says none of the skills it was shown fits), or the best meaning score is below `answer.refuse_below` when that is set. The checks run in this order: an empty result, an exact name, an accepted AI result, then the `answer.refuse_below` gate; so the gate applies to free results and never overrules a result the AI ranking chose. While `answer.refuse_below` is off, a hit found only in a skill's instructions text is never a no-match; with it on, the gate runs first and can refuse that hit. On our test tasks, "a query word is in a name or description" was true of 30 right first results, 24 wrong first results and 25 tasks that have no skill: it was wrong more often than right, which is why the free search says `possible` and not `good`. This is today's rule, not a measured relevance threshold; a new ranking rule replaces that one function.

The path after `read:` is the real path of `SKILL.md`, shown only while it is a file inside its own source folder; otherwise, and when the path holds a control or direction character, the answer gives the `inspect` call instead. Control characters in names, summaries and relationships are shown as code points in the text; the structured record keeps the raw values.

## Optional AI ranking and existing environment variables

The user file's `[ranking]` table can override `model`, `enable_env`, `key_env`, `transport`, `endpoint`, `cache_dir`, `timeout_seconds`, `shortlist`, `wide_candidates`, `body_excerpt_chars`, `none_threshold`, `cache_views` and `cache_max_age_seconds`. `[ranking.instructions]` accepts `wide`, `verify` and `none_description`. `cache_dir` must be absolute; numeric values must be positive, counts integral, and the timeout and none threshold must remain within the limits in the ranking policy. Unknown ranking keys fail at settings load, even when ranking is off. Invalid optional ranking values report `ranking.reason = "configuration_error"` and retain the free path. Safety caps remain the packaged contract. Settings alone do not enable paid ranking.

`ranking.endpoint` comes only from the trusted user file, never from project files, the ranking pointer, search input or environment proxy settings. It must be HTTPS, with a hostname and valid port, without user information, query parameters or fragments. There are no redirects or automatic retries. Configure an endpoint only when you trust it to receive the selected key, queries and skill excerpts. `ranking.key_env` names the secret environment variable; its value is never a file setting. Existing managed transports retain their own controls.

## A local usage log (off by default)

`usage.enabled = true` makes Capalith add one line per search to a file on your own machine, so you can see how it is used. Nothing is sent anywhere. It is off by default, and only your own settings file can turn it on (a project file or a tool call cannot).

| Key | Default | Meaning |
|---|---|---|
| `usage.enabled` | `false` | write the log |
| `usage.dir` | empty | an absolute folder for it; empty means `$XDG_DATA_HOME/capalith/usage` (`~/.local/share/capalith/usage` when unset) |
| `usage.retention_days` | `30` | lines older than this many days are removed the next time a line is written (1 to 3650) |
| `usage.skill_names` | `false` | also write the names of the first three results (each name cut to letters, digits, `.`, `_` and `-`) |

Each line (in `usage.jsonl`) holds: the time in UTC to the second; the tool (`discover` for the MCP server, `cli` for the command line); the ranking path (`ai` or `free`, `none` when the call failed) and its reason code; how many results; the status word (`exact`, `good`, `possible`, `weak`, `close`, `no_match`, or `error`); the duration in milliseconds; the error code or `none`; and a random id of this install (kept in `install-id` in the same folder). It never holds your query, a skill's text, a path, your user name or your host name, and it holds a skill's name only when you set `usage.skill_names`. Values come from fixed lists or lowercase codes; anything else is written as `other`.

The folder is owner-only (folder mode 0700, files 0600, no symbolic link followed). If the folder is not yours or is open to your group or to others, nothing is written. Logging never fails or slows a search: a write that fails is dropped and counted, and `config_show` shows the count as `usage.write_failures` while the log is on.

`capalith usage` prints one line per day from the log and nothing else: searches, how many used AI ranking, how many were free (by reason), errors, and the median and 95th percentile duration. `capalith usage --json` prints the same as JSON. It needs no catalog (`--config PATH` selects a settings file).

What we measured. One machine, Linux, the program pinned to six cores, a 174-skill catalog, three runs of 400 searches each, timing the logging call that follows each search: median 0.14 to 0.15 ms and 95th percentile 0.16 to 0.17 ms (largest 1.5 ms, once), against a search median of 63 to 65 ms. That is about 0.2 percent of a search. Not measured: another machine, a very large log, many servers writing the same folder at the same moment, macOS, or a network file system.

## Sources, storage and Git

`sources.hosts` has entries for `claude`, `codex` and `hermes`. Each accepts `home_env` (an environment-variable name, or empty to disable that lookup), `home_default` (an absolute or `~` path), `user_dirs` (relative to that host home) and `project_dirs` (relative to each task workspace). Directory lists replace their defaults. Relative directory entries cannot escape with `..`. Claude and Codex project folders are checked from the workspace up to its Git root; Hermes project folders are checked at that root, as before. With no Git root, only the current workspace is checked, and Hermes project folders are omitted.

`sources.shared_user_dirs` lists absolute or `~` folders shared by hosts; `shared_project_dirs` lists workspace-relative shared folders. Both replace their defaults, so an empty list disables them. `sources.workspace_env` chooses the existing workspace environment variable; explicit connection/call context still takes precedence. `sources.extra` lists additional absolute or `~` folders after host locations. Missing extra folders fail setup rather than silently narrowing the catalog. `sources.hermes_config_max_bytes` is a positive bound on reading the current Hermes profile's skill-location configuration.

Default source priority is unchanged: shared and host workspace folders, shared user folders, legacy Codex user skills, Claude user skills, current Hermes profile skills and configured profile folders, then extras. Connection `--extra-source` overrides the file's extras; a permitted call's `extra_source_paths` replaces the current additions, `[]` clears them, and omission retains that selection. Changes to user source settings or storage location invalidate automatic selection and discard remembered call-selected roots. Explicit `--db` connections retain their selected database.

`storage.data_dir` is the catalog directory itself, with no suffix appended. Empty means `$XDG_DATA_HOME/capalith/catalogs`, falling back to `~/.local/share/capalith/catalogs`; a nonempty absolute or `~` path overrides that default. The three folder settings (`storage.data_dir`, `semantic.model.dir`, `ranking.cache_dir`) share one rule: the path must be absolute (after `~`), must not contain a `..` step, must not hold a NUL byte, and `~user` must name a known user; otherwise the file is refused with `settings_error` naming the setting. A group- or world-writable folder or parent is not refused: the default data folder behaves the same. `--db` overrides automatic storage. `storage.busy_timeout_seconds` applies to fresh and retained SQLite connections; it is nonnegative and must fit SQLite's signed 32-bit millisecond range. It does not set a search deadline. Model storage remains `semantic.model.dir` with `CAPALITH_SEMANTIC_MODEL` taking precedence. Choose `storage.keep_old_vectors = true` when clients using different semantic models intentionally share a catalog, to preserve each other's vectors.

`git.timeout_seconds` and `git.fetch_timeout_seconds` are positive finite durations for each native Git step and fetch respectively. Protocol restrictions, disabled hooks and credential helpers, file safety checks and catalog identity remain fixed contracts.

## Agent source selection

`security.allow_call_sources` defaults to `true`, preserving current behavior. Set it to `false` to ignore every source-selection argument in MCP calls: `source_paths`, `extra_source_paths`, `workspace` and `host_homes`. The server then uses user settings, process environment, server working directory and connection flags. Changing the switch clears earlier call-selected roots, so they cannot survive revocation. Other arguments such as query and limit still work, and unknown settings arguments are refused. This switch does not disable workspace folders that the trusted configuration itself selects; empty the appropriate project-directory lists to exclude them. `config_show` reports the switch under `security.allow_call_sources`. `ranking.endpoint` is checked when ranking runs, not when the file loads.

Existing process environment and flags retain precedence for their documented inputs:

| Input | Effect |
| --- | --- |
| `CAPALITH_SEMANTIC_MODEL` | Overrides `semantic.model.dir`. |
| `XDG_CACHE_HOME` | Base for default model and ranking cache directories. |
| `XDG_DATA_HOME` | Base for automatically selected catalog storage. |
| `CAPALITH_RANKING_CONFIG` | Existing trusted JSON transport pointer; its timeout, none threshold and cache directory override user ranking settings. |
| `CAPALITH_RANKING`, `TYPESAFE_API_KEY` | Existing built-in ranking opt-in and secret environment variable, unless their names are overridden in trusted ranking settings. |
| `--db`, source flags, host-home environment | Existing catalog and source-selection behavior; see the [README](../README.md). |

Keep secrets in the server's secret environment, never in TOML, JSON, skill text or search arguments. The managed ranking route retains its own credential and budget controls; see [ranking configuration](ranking.md).
