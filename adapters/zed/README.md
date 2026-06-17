# zed adapter

Zed treats MCP servers as "context servers" under the `context_servers`
key in its settings JSON. The writer ships the workspace form
(`.zed/settings.json`); to enable vouch in every Zed workspace, paste the
same block into `~/.config/zed/settings.json` instead.

```sh
vouch install-mcp zed --path .
```

If `.zed/settings.json` already exists, the writer skips it — merge the
`context_servers.vouch` block from this directory's `settings.json` into
your existing file by hand.
