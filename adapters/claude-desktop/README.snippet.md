# Claude Desktop install

This template is **not** auto-installed into Claude Desktop because the
config file is user-global, not project-local. To finish the install:

1. Locate Claude Desktop's config:
   - macOS:   `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - Linux:   `~/.config/Claude/claude_desktop_config.json`
2. If the file doesn't exist, copy `claude_desktop_config.json` from this
   directory to that path.
3. If it does exist, merge the `mcpServers.vouch` entry into the existing
   `mcpServers` map.
4. Restart Claude Desktop. Then test with `kb_status` in a new chat.

Set `VOUCH_KB_PATH=/abs/path/to/your-project/.vouch` in the `env:` block to
point Claude Desktop at a specific KB (otherwise vouch walks up from the
working directory it was launched in, which is rarely useful for a GUI app).
