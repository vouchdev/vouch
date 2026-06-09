# cline adapter

Wires vouch into Cline (the VSCode extension formerly known as
Claude Dev). Cline reads MCP servers from two places:

- **Workspace-scoped:** `.vscode/settings.json` under `cline.mcpServers`.
- **User-global:** `~/.cline/mcp_servers.json`.

```sh
vouch install-mcp cline --path .
```

T1 drops a project-local `.cline/mcp_servers.json` (paste-ready into the
user-global path), and T2 drops a `.vscode/settings.json` fragment that
takes effect for the current workspace as soon as Cline reloads.

If you have an existing `.vscode/settings.json`, the writer skips T2 — merge
the `cline.mcpServers` block from this directory's `vscode_settings.json`
into your existing file by hand.
