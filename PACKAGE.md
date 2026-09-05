# Capalith

Capalith is a local-first catalog for portable Agent Skills. It searches selected sources, reads stored skill files, follows declared relationships, and exposes four read-only tools through MCP over stdio.

The package installs two console commands:

- `capalith` runs the JSON command-line interface.
- `capalith-mcp` runs the stdio MCP server. It requires `--db` with the catalog database path.

The portable companion skill is stored at `capalith/skills/capalith` inside the installed Python package. Installing the Python package does not install or activate that skill in an agent host and does not change host configuration.

If semantic search is unavailable, SQLite BM25 handles the full query. Install the `semantic` extra only when local semantic search is needed.
