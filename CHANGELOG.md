# Changelog

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
