# Flathub new-application checklist (#211)
#
# Use this when opening https://github.com/flathub/flathub/new-application

## upstream

- repository: https://github.com/vouchdev/vouch
- license: MIT
- app id: com.vouchdev.vouch

## manifest source

Copy `desktop/flatpak/flathub/com.vouchdev.vouch.json` into the Flathub
application repo as `com.vouchdev.vouch.json`. Replace `REPLACE_WITH_TAG_COMMIT`
with the git object for the release tag you are shipping.

For day-to-day Flathub builds, prefer pinning `tag` + `commit` together so
rebuilds stay reproducible when tags move.

## permissions justification (for reviewers)

| finish-arg | why |
|---|---|
| `--share=network` | `vouch review-ui` binds a localhost port and the browser loads the UI |
| `--filesystem=home` | KB trees live under `$HOME` (project `.vouch/` dirs and optional `~/.vouch/`) |
| `--talk-name=org.freedesktop.portal.Desktop` | open the default browser to the review console |
| `--socket=wayland` / `fallback-x11` | standard GUI session integration |

Per-KB filesystem scoping is explicitly out of scope for v1 (#211).

## store listing assets

- icons: `desktop/flatpak/share/icons/hicolor/*/apps/com.vouchdev.vouch.png`
- metainfo: `desktop/flatpak/com.vouchdev.vouch.metainfo.xml`
- screenshots: capture with `scripts/capture-screenshots.sh` after `flatpak run`

## acceptance (#211)

- `flatpak install flathub com.vouchdev.vouch` on Fedora 40+ and Arch
- Flathub listing shows icon, description, screenshots from metainfo
