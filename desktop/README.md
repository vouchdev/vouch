# vouch — desktop AppImage

Self-contained Ubuntu (x86_64) launcher for `vouch review-ui`. One file. No
`apt install`. Double-click to start the browser-based review console.

## What this directory is

```
desktop/
  requirements.txt           # vouch-kb[web] (default) or local+vouch for pre-release
  vouch-review-ui.desktop    # Linux menu entry — Terminal=false, Categories=Development
  vouch-review-ui.svg        # scalable monograph icon
  entrypoint.sh              # auto-inits ~/.vouch/ on first run, hands off to `vouch review-ui`
```

The bundler is [`python-appimage`](https://github.com/niess/python-appimage),
which downloads a relocatable CPython, pip-installs the requirements into it,
wraps the `entrypoint.sh` in an AppRun, and emits a single `.AppImage`.

## Build (release, from PyPI)

```bash
pip install python-appimage
python -m python_appimage build app desktop/ \
  --python-version 3.12 \
  --linux-tag manylinux2014_x86_64
# → desktop-x86_64.AppImage in cwd
mv desktop-x86_64.AppImage vouch-review-ui-x86_64.AppImage
chmod +x vouch-review-ui-x86_64.AppImage
```

Requires `vouch-kb[web]` to be published on PyPI — that lands once PR #195
ships the `[web]` extra in the next vouch-kb release.

## Build (pre-release, from local source tree)

Until the next vouch-kb release, swap the requirements entry to bundle the
working-tree source:

```bash
# in desktop/requirements.txt, replace `vouch-kb[web]` with:
local+vouch
# (plus any deps the [web] extra would pull):
fastapi>=0.115,<1
jinja2>=3,<4
python-multipart>=0.0.9
uvicorn>=0.30,<1

# then build as above
python -m python_appimage build app desktop/
```

## Launch

```bash
./vouch-review-ui-x86_64.AppImage
# or double-click in the file manager
```

First launch creates `~/.vouch/` if it doesn't exist (an empty KB with the
starter claim), binds `127.0.0.1:7780`, and opens the browser to the review
queue. Override the KB location with `VOUCH_KB_PATH=/path/to/parent` in the
environment.

## Stop

The AppImage runs a foreground uvicorn process. Close the launching
terminal, or `pkill -f vouch-review-ui`. A system-tray quit menu is a
natural follow-up but out of scope for the first release.

## What's not in this release (yet)

- `.deb` package — the AppImage covers the Ubuntu story for now.
- Snap / Flatpak — needs separate confinement work; deferred.
- System-tray quit / auto-start on login — needs a native shell (Tauri/Electron); deferred.
- Auto-update — once the GH Actions release workflow lands, [AppImageUpdate](https://github.com/AppImage/AppImageUpdate) can sit on top.
- ARM64 / aarch64 builds — `--linux-tag manylinux2014_aarch64` once we have a runner.

## How the install + run resembles `pipx install vouch-kb`

Same Python, same `vouch` command, same `.vouch/` layout — the AppImage is
just a bundled CPython + entry-point. Power users keep using `pipx`; the
AppImage is for users who want a single download and a menu entry.
