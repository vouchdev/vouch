# claude-desktop adapter

Wires vouch into Anthropic's Claude Desktop app via Claude Desktop's MCP
config file. Unlike Claude Code (project-local `.mcp.json`), Claude Desktop
reads a single user-global JSON, so the writer drops a reviewable copy
under `<project>/claude-desktop/` and the user copies it into place — see
the snippet for OS-specific paths.

```sh
vouch install-mcp claude-desktop --path .
# then follow the printed README.md to finish the install
```
