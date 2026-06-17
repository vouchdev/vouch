# windsurf adapter

Codeium's Windsurf IDE reads MCP servers from
`<project>/.codeium/windsurf/mcp_config.json` (workspace-scoped) or
`~/.codeium/windsurf/mcp_config.json` (user-global). The writer installs
the workspace form; if you want vouch available in every Windsurf workspace,
copy the same JSON into the user-global path manually.

```sh
vouch install-mcp windsurf --path .
```

Then reload the Windsurf workspace. `kb_status` should appear in the MCP
tools list.
