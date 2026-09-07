# Changelog

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
