# vouch Flatpak (#211)

Sandboxed Linux install for the **vouch review console** (`vouch review-ui`).
Targets Fedora 40+, Arch, and any desktop with Flathub.

## Quick start

```bash
# one-time runtime (org.freedesktop.Platform//23.08 + Sdk; python3 is in the Sdk)
./scripts/install-runtime.sh

# build + install for the current user
make install

# launch from the app menu or:
flatpak run com.vouchdev.vouch
```

## Layout

| Path | Purpose |
|---|---|
| `com.vouchdev.vouch.yaml` | flatpak-builder manifest (local dev build) |
| `com.vouchdev.vouch.desktop` | Freedesktop launcher |
| `com.vouchdev.vouch.metainfo.xml` | AppStream metadata for Flathub |
| `vouch-review-ui.sh` | `/app/bin` entrypoint |
| `share/icons/hicolor/` | App icons (16–512 + scalable SVG) |
| `flathub/` | Flathub submission JSON + checklist |
| `lib/validate.py` | Packaging validators (pytest + CLI) |
| `scripts/` | build, icon generation, screenshot capture |

## Permissions (v1)

| finish-arg | rationale |
|---|---|
| `--share=network` | uvicorn binds localhost; browser loads the UI |
| `--filesystem=home` | read/write `.vouch/` KB trees under `$HOME` |
| `--talk-name=org.freedesktop.portal.Desktop` | open the default browser |

Per-KB filesystem scoping is **out of scope** for v1 — see [issue #211](https://github.com/vouchdev/vouch/issues/211).

## Develop

```bash
make icons       # regenerate PNGs from SVG
make validate    # strict manifest/desktop/metainfo checks
make requirements  # sync requirements-flatpak.txt from pyproject.toml
make build       # flatpak-builder only (no install)
```

From repo root:

```bash
python -m pytest tests/test_flatpak.py -q
```

## Flathub

After merge, follow `flathub/SUBMISSION.md` to open a Flathub application PR.
The store listing reuses metainfo screenshots under `screenshots/` — capture with
`scripts/capture-screenshots.sh` once review-ui is installed.

Acceptance:

- `flatpak install flathub com.vouchdev.vouch` on Fedora 40+ and Arch
- listing shows icon, description, and screenshots

## Runtime

- **Base:** `org.freedesktop.Platform//23.08`
- **Python:** bundled in `org.freedesktop.Sdk` (no separate Extension.python3 on Flathub)
- **Package:** `pip install '.[web]'` — no bundled CPython upgrade automation
