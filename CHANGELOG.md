# Changelog

## 0.2.2 (release candidate, not released)

- **A common word no longer makes a false `possible`.** A search such as "book a dentist appointment" answered `possible` for a violin skill and a tide-table skill because the word "a" is in their descriptions. Now a word that is on the common-word list (`answer.hide_words`, shipped with a default; empty it to get the old rule) counts toward `possible` only when it is in a skill's name. The results and their order do not change, only the status word: on our 61 labelled development queries 8 first results moved from `possible` to `weak`, all of them wrong first answers, and none of the 29 right first answers changed.
- **The `weak` line matches its tag.** It said no word of yours was in the name or description while the tag named a word found in the body. It now says the words are only in the body, or that the only words shared are common ones. The sentences are settings (`answer.signals`).
- **A common word no longer makes a false `possible`.** A search such as "book a dentist appointment" answered `possible` for a violin skill and a tide-table skill because the word "a" is in their descriptions. Now a word that is on the common-word list (`answer.hide_words`, shipped with a default; empty it to get the old rule) counts toward `possible` only when it is in a skill's name. The results and their order do not change, only the status word: on our 61 labelled development queries 8 first results moved from `possible` to `weak`, all of them wrong first answers, and none of the 29 right first answers changed.
- **A local usage log, off by default.** With `usage.enabled = true` in your settings file, each search adds one line to a file in an owner-only folder on your machine: time (UTC), tool, ranking path and reason, number of results, status word, duration in ms, setup error code or `none`, and a random id of the install. It never holds the query, a skill's text, a path, a user name or a host name (skill names only if you set `usage.skill_names`). Nothing is sent anywhere. `capalith usage [--json]` prints one line per day: searches, AI and free (by reason), errors, median and 95th percentile duration. Old lines are removed after `usage.retention_days` (default 30). Logging never waits (its locks do not block): a line that cannot be written, including one that meets a trim in progress, is dropped and counted (`config_show` shows `usage.write_failures` while the log is on), and the folder is opened without following a symbolic link. The reason in a line comes from a fixed list of codes, anything else is written as `other`, and a failed command-line search is counted like a failed MCP call. Cost on one busy machine (174 skills, pinned to six cores, 1,200 searches): the logging call took a median of 0.18 ms (95th percentile 0.21 ms) against a search median of 65 ms, with rare slow calls of 13 to 234 ms, cause not found. Everything else is as in 0.2.1.

## 0.2.1, 2026-11-25

The first public release after 0.1.3. It includes everything in the sections below for 0.2.0, 0.1.5 and 0.1.4, which were never published on their own: read "Before you upgrade" under 0.2.0 first. What 0.2.1 adds on top of them:

- **`--version`.** `capalith`, `capalith-mcp` and `capalith-model` print their name and version and exit 0 (0.1.3 exits 2).
- **A catalog from a newer Capalith says so.** Opening a catalog file whose format is newer than this version reads now fails with `catalog_newer: this catalog was written by a newer Capalith (catalog format N; this version reads up to M). Upgrade Capalith, or point at another catalog.` (the MCP call, the server's startup message and the command line). In 0.1.3 the MCP call says `catalog_setup_failed` with no cause (or `database_unavailable` on a `--db` server), and the command line prints only `unsupported store schema version: N`. The text is fixed and holds only the two numbers; every other setup error is unchanged and still redacted.
- **Lower memory for an idle server.** The search model (about 115 MiB) is dropped after `semantic.idle_release_seconds` without a search (default 300; 0 keeps it for good), the memory is returned to the system where the platform allows it (glibc `malloc_trim`; nothing fails elsewhere), and the model is rebuilt on the next search, which then takes about 0.10 to 0.12 s longer (0.14 to 0.16 s against about 0.04 s warm, in eight runs on a 174-skill catalog). A search never loses its model mid-call: one lock covers the model's use and its release. Results are unchanged (the same vectors after a reload; all 122 results of the 61 development queries identical with the setting on). Measured on real MCP servers with a 3 s idle time on a 174-skill and a 10,125-skill catalog: one idle server went from 241 to 128 MiB and from 319 to 199 MiB of proportional memory, and five idle servers from 1,060 to 608 MiB and from 1,419 to 846 MiB. The setting is not part of the index key, so no catalog is re-indexed.

## 0.2.0 (not published; included in 0.2.1)

### Before you upgrade

- **One-way catalog upgrade.** The first run of 0.2.1 moves an existing 0.1.3 catalog from schema 7 to 8 in place (stored vectors are kept byte for byte; model-dependent vector sizes become possible); a read-only `--db` server does not move it, it refuses. Older versions refuse a schema 8 catalog, so every client that shares a catalog must upgrade together; keep a backup of the catalog file first.
- **MCP `discover` answers differently by default.** It returns short plain text (a status, then per skill a name, one-sentence summary, reason and the absolute `SKILL.md` path; three skills by default) with a small structured record beside it, not the full record with IDs, digests, excerpts and scores. Ask for the full record with `detail: "full"` on the call or `answer.detail = "full"` in the settings file. The CLI is unchanged.
- Everything else defaults to today's behaviour: with no settings file, search order, the embedding model and the 0.66 gate are as in 0.1.3 (the last release before this one; the same top five on our 91 development queries). New settings live in one user-owned file; a project folder can never supply settings.

### The agent's side

- The `discover` description and the first line of the server instructions now say when to call it (at the start of a task no known skill covers) and when not to.
- Results say `exact`, `possible`, `weak`, `close`, `no_match` or, only where something judged it, `good`. The rule is one function (`capalith.answer.judge`) over the signals the search has. On the development sets the free search's first result is right 30 times in 56 answerable tasks, and the old "good" (a query word in a name or description) sat on a wrong first result 24 times and on a no-answer task 25 times, so on the free search a name or description word is now `possible`. `good` appears when the AI ranking chose the result, or when you set `answer.good_from` (off by default; 0.70 measured 13 right and 2 wrong first results of 87 cases, 0.67 measured 22 right and 5 wrong). `weak` is a first result with no query word in its name or description, and its message says whether only the meaning or only the body relates; `close` is that case with a near tie (absolute gap); `no_match` is an empty result or, when `answer.refuse_below` is set, a low best meaning score.
- The agent's text is shorter and plainer: reasons read "matched by meaning and your words 'a', 'b'"; no_match ends "Carry on without one." and no longer prints the rule's own line (it stays in the structured record as `signal`); more function words are hidden. The `discover` description says when to call it, and its argument details moved to the parameters.
- AI ranking works at any catalog size. Its first call used to send every skill, but the API takes at most 255 choices and the request is capped at 240,000 bytes, so above a few hundred skills it silently fell back to the free search. Now, when the searched skills fit (at most `ranking.wide_candidates`, default 200, and the first request, the second (verification) request in its worst case and the worker payload are each under their byte caps), the request is exactly as before; when they do not, the first call sends the free search's own top N for that query (lexical and meaning), trimmed from the bottom until all three fit, never an error, and no paid call is spent on an answer that could not be verified. When the meaning side is not in use (the index is off or stale, or the model is unavailable) and the catalog is larger than `ranking.wide_candidates`, a query that the whole word search finds nothing for (it reads each skill's name, description and instructions, and the text and paths of its other stored files) sends nothing and the free search answers, so a task that matches only by meaning can be missed then; a catalog within `wide_candidates` is still sent whole. The result reports `ranking.roster` (`total`, `sent`, and why it was trimmed), and a no-skill answer from a trimmed roster says "None of the N nearest skills fits this task". When the whole catalog is within `wide_candidates` and only the byte caps cut it, the free search's candidates come first and the rest fill in name order as far as the caps allow. An accepted AI result is judged before the local `refuse_below` gate. The two-call design and its instructions are unchanged.
- Three newcomer errors say what to do: `settings_untrusted` names the path, why, and the one `chmod go-w` that fixes it (shell-quoted; no command is offered for a name with control characters; the check is not loosened; control characters in any path shown are written as code points); `no_skill_sources` lists the folders it looked in, built from the configured hosts, and points to `--extra-source`; `capalith-model provision` says before it starts what it downloads, its size and where it goes.
- The short answer lists three skills by default (`answer.default_limit`); a call's `limit` and `detail: "full"` keep today's page size.
- The read path is the real path of `SKILL.md` and is shown only while it is a file inside its source; control and direction characters never reach the answer text; the character budget holds for long paths too (a path that does not fit is said as too long, never cut); a redirect is shown only for a skill found by exact name in the searched sources.
- A skill description's "for X use Y" sentence lists Y after the page when X matches the task. Rankings and result order are unchanged.
- New `answer` settings; see docs/settings.md.
- Selectable embedding model: `semantic.profile = "arctic-embed-s"` (pinned, hash-checked). New, off by default: `answer.refuse_below` makes a search say `no_match` when its best meaning score is below the value; the search result now reports `semantic.best_similarity`. The default model and behaviour are unchanged.

### Speed at 100,000 skills

- The word and meaning passes of a search now order their rows in arrays instead of sorting and fusing each row in Python. The word search runs once without an `ORDER BY` (it used to run twice when more than `search.lexical_candidates` rows matched), and a very large `search.rrf_k` is added in whole numbers, as before.
- The order is the previous order. On the final code, every checked page matched the previous code exactly: the 61 development queries at 174 skills (six pages) and at 100,125 skills (three pages), the same queries on a generated catalog with about 1,000 skills passing the meaning gate (two pages), 2,400 random queries over tie-heavy random catalogs (15,989 pages), the order of the skills the AI ranking is offered, and the 122 request bodies. A test in the repository compares 300 more such queries with a file made by the previous code.
- Speed, measured on generated catalogs (231 real skills plus 100,000 generated ones, with generated text and vectors) and the 61 development queries, on a busy 16-core machine that was never quieter than a load of 6. On the final code the 95th percentile was 0.52 s at a load of 16.5; the previous code, run right after at a load of 21.1, took 1.50 s (median 0.18 s for both). An earlier build of the same change took 0.45 s against 1.57 s at a load of 12 (median 0.16 s against 0.19 s). With generated meaning lists in which about 1,000, 10,000 and 50,000 skills pass the gate, the 95th percentile was about 0.44 to 0.46 s (the previous code took 1.73 s at the 1,000 size; the other two sizes were not timed on it). The two slowest of the 61 queries took about 0.5 to 0.6 s in every run.
- The first search after start builds integer sort keys once per catalog and takes about 7.6 to 8.4 s at 100,000 skills (6.3 to 6.4 s before); peak memory was about 30 MB higher in one measurement and about 100 MB in another, on different generated catalogs; the difference isn't explained. A 95th percentile under 500 ms is not claimed: it held at loads of 9 to 12 and not at 16.5, and no quiet machine and no real 100,000-skill catalog were measured.
- `search.lexical_candidates` is kept so existing settings files still load, but it no longer limits the word search: every match is read once.

### Platforms and CI

- Supported: Linux on x86_64 with CPython 3.11. That is the only combination the required CI job proves; a green overall result means that and nothing more, because the other jobs are allowed to fail.
- **macOS is experimental and unsupported.** The catalog opens by the folder's path (`F_GETPATH`) instead of Linux's held folder, which is weaker: a persistent folder or file swap is refused, but a swap that is restored before the checks, a swap after them and the journal names SQLite opens by path are not caught. No Mac has run it; a test keeps the restored-swap case as a known limit.
- Windows through WSL2 is expected to work as Linux and is not yet proven. Native Windows is not supported.
- New CI jobs, each read on its own before anything is called supported: Python 3.12 and 3.13 on Linux, macOS arm64 with 3.11 and 3.12, and WSL2. Hash-pinned requirement files exist per target (`requirements-<target>.txt`), written by `tools/lock_hashes.py` from public PyPI metadata (an unreadable requirement is an error that names the line, and a Python 2 only wheel is never taken as a fit), and every GitHub Action is pinned to a full commit that `tools/check_action_pins.py` checks against its version comment.

### Settings and storage

- Add trusted user settings for sources and host folders, catalog location and SQLite waits, Git deadlines, user-only ranking endpoints and control over agent-supplied source folders. Defaults preserve existing behavior; project files cannot supply settings.

## 0.1.5 (not published; included in 0.2.1)

### Ranking worker isolation and clearer setup

- Load the ranking worker's Capalith package directly without adding its containing folder to module lookup. Explicit absolute host transport roots remain supported.
- Bound worker output with the shipped policy and stop, kill and reap an oversized worker before falling back to free results. Input and output retain the shared deadline and accounting grace.
- Report `ranking.reason` as `missing_key` when TypeSafe is explicitly enabled but the server receives no key or a blank key; keep `not_configured` for a disabled route. The README explains the missing-key case.
- Clarify that spending uses organization credits and organization members can see the key list. Explain TypeSafe's prepaid balance, optional auto-recharge, invoice billing on request and HTTP 402 when credits run out; the balance limits prepaid spending unless auto-recharge or invoice billing applies.

The two-stage ranking policy and measured relevance are unchanged. No faster ranking method or new live latency result is claimed.

## 0.1.4 (not published; included in 0.2.1)

### AI ranking with your own TypeSafe key

- Bring your own dedicated TypeSafe API key: validate it with the README's free curl models check, then set `CAPALITH_RANKING=typesafe` and `TYPESAFE_API_KEY` only in the MCP server's own secret or environment entry. Agent tools could read a shell or server environment, and a malicious skill could try. Monitor usage and revoke the dedicated key to stop further spending; there is no built-in spending cap or documented TypeSafe per-key spend limit. The built-in HTTPS transport needs no custom adapter and sends the key only to TypeSafe, never to results, errors, logs or the cache. No key is included or shared. Existing host-managed transports keep working and take precedence when explicitly configured.
- Keep local ranking as the fallback for missing configuration, provider errors, deadline expiry or an unavailable cache. Results report `ranking.path` as `ai` or `free` and explain a fallback with `ranking.reason`.
- Return no candidates or recommendations when AI ranking judges that none of the offered skills fits the request. No automatic prompt hints or background provider calls are added.
- Preserve accepted rankings across CLI calls and MCP reconnects in a separate private view cache. Views expire after the policy's one-hour maximum age; expired raw queries are securely deleted on the next cache access, while idle files persist until then.
- Require the ranking configuration and its containing directory to belong to the caller and have no group or world write permission. Start the worker with safe module lookup and allow a short accounting grace after its request deadline.
- Mark `discover` as writing and non-destructive in every connection mode because it can maintain the ranking cache. Existing read-only catalog modes remain available.

A frozen prerelease host-managed evaluation found a relevant skill in the top three for 16–17 of 22 requests, compared with 2 of 22 using the original local ranking, and abstained on 3 of 4 no-skill requests. Full-search medians were about 2.5–2.9 seconds. These are repeated calibration-set results, not an unseen holdout; the implementation reproduced all 78 saved discovery results, without a new live latency measurement after the accounting-grace or built-in transport changes.

Enabled AI search sends the query, skill names and descriptions, and shortlisted instruction excerpts to its provider. At TypeSafe's published $0.042 per million input tokens, 20,000 combined tokens across the usual two requests cost $0.00084; output tokens are free. The shipped policy caps each request at 240,000 bytes (240 KB), roughly 60,000 input tokens: about $0.0025 per request or $0.005 per two-request search. Tokenization varies, so this byte cap is not an exact token or dollar cap. There is no limit on how many searches an agent makes; only identical queries with unchanged catalog, scope and configuration reuse an unexpired cached view. Cached continuation makes no provider request. An interrupted request or failed cache write can still leave a charge. The built-in route follows no redirects and has no endpoint override, environment proxy or automatic paid retry.

A valid-key search through the built-in route was not tested live before release; transport checks used a local stub. The free models check validates model-list access, not a paid search.

## 0.1.3, 2026-09-10

### First-search setup and additional folders

- Prepare the default MCP catalog on the first `discover` call, with task and profile context accepted on search. Repeated searches read the stored snapshot; explicit refresh remains available.
- Add `--extra-source` connection arguments and `extra_source_paths` tool input for folders alongside discovered native locations. Preserve explicit-only and read-only modes.
- Remove fallback-only usage guidance. Capalith provides catalog navigation without prescribing a search-first workflow or adding a host plugin.
- Validate conflicting source inputs in the tool handler so hosts that rewrite JSON Schema `not` clauses retain the same input checks.

Default-mode `discover` now advertises catalog writes. Sources and host configuration remain unchanged, and no skill is automatically loaded or activated.

## 0.1.2, 2026-09-06

### Focused skill reading

- Clarify when to reuse loaded instructions, use a permitted native reader, or retrieve a stored revision. Full inspection and duplicate content loading are no longer prescribed for ordinary skill use. Addresses [#1](https://github.com/Kayhusk/capalith/issues/1).
- Resource-specific inspection now returns artifact/source identity, presence and size fields, catalog status, and the selected resource. It no longer builds or returns manifests, version history, the full resource list, or relationships. Omit `resource_path` in MCP or the optional resource argument in the CLI to retain full audit inspection. Addresses [#2](https://github.com/Kayhusk/capalith/issues/2).

The resource-specific response shape changes in this release. Consumers needing audit fields must use full inspection. Tool names, arguments, validation, stored content, and catalog format remain unchanged. No ranking, semantic-model, background-refresh, activation, or host-configuration changes are included.

## 0.1.1, 2026-09-06

### Full installation

- Include semantic-search dependencies in the standard package rather than an optional extra.
- Add `capalith-model provision` and `capalith-model verify`, using the existing pinned-model download and hash-verification implementation.
- Document one full installation with the CLI, MCP server, bundled guidance, and local semantic model. SQLite BM25 remains the recovery path if semantic search is unavailable.

### Agent-led setup and context fixes

- Start the default MCP server without database or source arguments. Expose `index_skills` for first-use setup and refresh while keeping retrieval tools read-only.
- Discover supported host skill locations independently of the package installation directory. Accept the agent's actual workspace and known custom host homes.
- Handle Hermes profile-relative external directories, `skills.create_dir`, and linked category and skill directories. Continue rejecting supporting-file links inside skill bundles.
- Retain workspace and host-home context during refresh. Switch project selection without reconnecting. Use Claude Code's supplied project directory before falling back to the server working directory.
- Preserve explicit-source startup and existing-catalog read-only connections.

### Verification and release evidence

The [v0.1.1 release](https://github.com/Kayhusk/capalith/releases/tag/v0.1.1) carries the installable wheel, source distribution, SHA-256 checksums, and a verification record. The record separates source tests, installed-artifact tests, model verification, and named-host checks. CI is tied to the release commit.

Prior Hermes and Codex context pilots verified separate installation and workspace locations, project switching, and exact stored-skill readback. Those pilots do not prove complete native host inventories or guarantee an agent will choose Capalith. Live Claude Code acceptance is not claimed. Orion deployment evidence is recorded separately from package acceptance.

This is an alpha release with the complete shipped feature set. It does not change host trust decisions, activate retrieved skills, modify host core, or install a background service.
