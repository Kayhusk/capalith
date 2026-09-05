# Capalith

Capalith is a local-first catalog for portable Agent Skill bundles. It provides scoped discovery, exact stored-resource inspection, declared relationship traversal, and a read-only stdio MCP server.

The package installs two console commands:

- `capalith` runs the JSON command-line interface.
- `capalith-mcp` runs the stdio MCP server. It requires `--db` with the catalog database path.

The portable companion skill is stored at `capalith/skills/capalith` inside the installed Python package. Installing the Python package does not install or activate that skill in an agent host and does not change host configuration.

SQLite BM25 is the complete retrieval fallback. Install the `semantic` extra only when local semantic retrieval and model provisioning are required.