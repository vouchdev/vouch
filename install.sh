#!/bin/sh
# vouch installer — pipx-backed, no sudo.
#
#   curl -fsSL https://raw.githubusercontent.com/vouchdev/vouch/main/install.sh | sh
#
# What this does, in order:
#   1. Pick a Python interpreter (>=3.11) — exits with a hint if none found.
#   2. Make sure pipx is installed (user-scope install if missing; falls
#      back to a private venv on PEP 668 externally-managed systems).
#   3. Install or upgrade the `vouch-kb` package via pipx.
#   4. Smoke-test: run `vouch --version` and report success.
#   5. If Claude Code config is detected, point you at `vouch install-mcp`.
#
# Safe to re-run. Nothing requires sudo. No network calls beyond pipx's
# normal PyPI fetch.
#
# Flags:
#   --version <X.Y.Z>   pin a vouch-kb version (default: latest)
#   --no-claude         skip the Claude Code detection nudge
#   --quiet             only print errors + the final summary
#   --help              print this message

set -eu

# --- knobs ---------------------------------------------------------------

PKG_NAME="vouch-kb"
PIN_VERSION=""
SKIP_CLAUDE_CHECK=0
QUIET=0
MIN_PY_MAJOR=3
MIN_PY_MINOR=11
REPO_URL="https://github.com/vouchdev/vouch"

# --- pretty output -------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_BOLD=$(printf '\033[1m')
    C_DIM=$(printf '\033[2m')
    C_RED=$(printf '\033[31m')
    C_GREEN=$(printf '\033[32m')
    C_YELLOW=$(printf '\033[33m')
    C_BLUE=$(printf '\033[34m')
    C_RESET=$(printf '\033[0m')
else
    C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_RESET=""
fi

info() {
    [ "$QUIET" -eq 1 ] && return 0
    printf '%s▸%s %s\n' "$C_BLUE" "$C_RESET" "$1"
}

warn() {
    printf '%s!%s %s\n' "$C_YELLOW" "$C_RESET" "$1" 1>&2
}

err() {
    printf '%s✗%s %s\n' "$C_RED" "$C_RESET" "$1" 1>&2
}

ok() {
    [ "$QUIET" -eq 1 ] && return 0
    printf '%s✓%s %s\n' "$C_GREEN" "$C_RESET" "$1"
}

has_cmd() {
    command -v "$1" >/dev/null 2>&1
}

usage() {
    cat <<EOF
${C_BOLD}vouch installer${C_RESET}

  curl -fsSL https://raw.githubusercontent.com/vouchdev/vouch/main/install.sh | sh

Usage:
  install.sh [--version X.Y.Z] [--no-claude] [--quiet] [--help]

What it does:
  1. picks a python >=3.${MIN_PY_MINOR}
  2. installs pipx (user scope) if missing
  3. ${C_BOLD}pipx install ${PKG_NAME}${C_RESET} (or upgrade if already present)
  4. smoke-tests with ${C_BOLD}vouch --version${C_RESET}
  5. points you at Claude Code wiring if applicable

Re-run safely. No sudo. No network beyond pipx + PyPI.

Source: ${REPO_URL}/blob/main/install.sh
EOF
}

# --- arg parsing ---------------------------------------------------------

while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            shift
            [ $# -gt 0 ] || { err "--version needs a value"; exit 2; }
            PIN_VERSION="$1"
            ;;
        --no-claude)
            SKIP_CLAUDE_CHECK=1
            ;;
        --quiet)
            QUIET=1
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            err "unknown flag: $1"
            usage
            exit 2
            ;;
    esac
    shift
done

# --- phase 1: pick a Python ----------------------------------------------

pick_python() {
    # Try, in order: python3.13, 3.12, 3.11, then bare python3.
    for cand in python3.13 python3.12 python3.11 python3; do
        if has_cmd "$cand"; then
            ver=$("$cand" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null) || continue
            major=${ver%%.*}
            minor=${ver##*.}
            if [ "$major" -gt "$MIN_PY_MAJOR" ] || \
               { [ "$major" -eq "$MIN_PY_MAJOR" ] && [ "$minor" -ge "$MIN_PY_MINOR" ]; }; then
                printf '%s\n' "$cand"
                return 0
            fi
        fi
    done
    return 1
}

# --- phase 2: ensure pipx -------------------------------------------------

ensure_pipx_bin_on_path() {
    # PIPX_BIN_DIR (typically ~/.local/bin) holds the `vouch` shim that pipx
    # writes. On many Ubuntu/Debian setups it's NOT on PATH by default even
    # when pipx itself is — the shim exists, smoke_test would still fail.
    # Run this unconditionally so re-runs against a pre-installed pipx
    # still produce a usable shell.
    py="$1"
    pipx_bin=$("$py" -m pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)
    if [ -z "$pipx_bin" ]; then
        pipx_bin="$HOME/.local/bin"
    fi
    case ":$PATH:" in
        *":$pipx_bin:"*) ;;
        *) PATH="$pipx_bin:$PATH"; export PATH ;;
    esac
}

ensure_pipx() {
    py="$1"
    # Put the usual pipx bin dir on PATH *before* probing: a fresh
    # non-login shell often misses ~/.local/bin, so an already-installed
    # pipx would look absent and get shadowed by a needless reinstall.
    ensure_pipx_bin_on_path "$py"
    # `pipx --version` and not just `has_cmd`: a leftover shim whose venv
    # died (e.g. a brew python upgrade) must fall through to the
    # recreation path below, not get accepted as installed.
    if has_cmd pipx && pipx --version >/dev/null 2>&1; then
        ok "pipx already installed ($(pipx --version 2>/dev/null | head -1))"
        return 0
    fi
    info "pipx not found — installing into user site (no sudo)"
    pipx_log=$(mktemp "${TMPDIR:-/tmp}/vouch-install-pipx.XXXXXX") || {
        err "could not create the pipx install log"
        return 1
    }
    if "$py" -m pip install --user --upgrade pipx >"$pipx_log" 2>&1; then
        "$py" -m pipx ensurepath >/dev/null 2>&1 || true
    else
        # Common causes: PEP 668 externally-managed interpreters
        # (Debian 12+ / Ubuntu 23.04+ / Homebrew) refusing --user, or a
        # missing pip module. Host pipx in a private venv instead —
        # still no sudo.
        info "user-site install failed — using a private venv instead"
        pipx_venv="$HOME/.local/share/vouch/pipx-venv"
        # Recreate from scratch: brew pythons ship read-only activate
        # scripts, so `python -m venv` over a previous run's venv dies
        # with EACCES.
        rm -rf "$pipx_venv"
        if ! "$py" -m venv "$pipx_venv" >>"$pipx_log" 2>&1 || \
           ! "$pipx_venv/bin/pip" install --quiet --upgrade pipx >>"$pipx_log" 2>&1; then
            err "could not install pipx (user site failed, private venv failed)"
            err "last errors ($pipx_log):"
            tail -5 "$pipx_log" 1>&2 || true
            err "install it manually, then re-run:"
            err "  sudo apt install pipx python3-venv   # Debian/Ubuntu"
            err "  brew install pipx                    # macOS"
            return 1
        fi
        PATH="$pipx_venv/bin:$PATH"; export PATH
        # keep pipx reachable in later shells too (~/.local/bin is what
        # `pipx ensurepath` puts on PATH) — but never shadow a pipx the
        # user already has there.
        mkdir -p "$HOME/.local/bin"
        if [ ! -e "$HOME/.local/bin/pipx" ] && [ ! -L "$HOME/.local/bin/pipx" ]; then
            ln -s "$pipx_venv/bin/pipx" "$HOME/.local/bin/pipx"
        fi
        pipx ensurepath >/dev/null 2>&1 || true
    fi

    # `ensurepath` edits ~/.profile / ~/.zshrc to add pipx's bin dir, but
    # the current process's PATH is already fixed. Add it explicitly so
    # the rest of this script can find `vouch`.
    ensure_pipx_bin_on_path "$py"

    if ! has_cmd pipx; then
        err "pipx installed but not on PATH — restart your shell, then re-run"
        return 1
    fi
    ok "pipx ready at $(command -v pipx)"
    return 0
}

# --- phase 3: install vouch-kb -------------------------------------------

install_vouch() {
    target="$PKG_NAME"
    if [ -n "$PIN_VERSION" ]; then
        target="${PKG_NAME}==${PIN_VERSION}"
    fi

    already_installed=0
    if pipx list 2>/dev/null | grep -q "package $PKG_NAME"; then
        already_installed=1
    fi

    if [ "$already_installed" -eq 1 ] && [ -n "$PIN_VERSION" ]; then
        # --version was requested AND the package is already installed:
        # `pipx upgrade` would ignore the pin and pull latest. Force a
        # clean reinstall to the exact pin instead.
        info "re-installing $target (honouring --version pin)"
        pipx install --force "$target" >/dev/null
    elif [ "$already_installed" -eq 1 ]; then
        info "upgrading existing $PKG_NAME"
        if ! pipx upgrade "$PKG_NAME" >/dev/null 2>&1; then
            warn "pipx upgrade failed — falling back to reinstall"
            pipx install --force "$target" >/dev/null
        fi
    else
        info "installing $target"
        pipx install "$target" >/dev/null
    fi
    ok "$PKG_NAME installed"
}

# --- phase 4: smoke test --------------------------------------------------

smoke_test() {
    if ! has_cmd vouch; then
        err "vouch command not found after install — check pipx's bin dir is on PATH"
        return 1
    fi
    if ! ver=$(vouch --version 2>&1); then
        err "vouch installed but \`vouch --version\` failed:"
        err "  $ver"
        return 1
    fi
    ok "$ver"
    return 0
}

# --- phase 5: Claude Code nudge ------------------------------------------

claude_code_nudge() {
    [ "$SKIP_CLAUDE_CHECK" -eq 1 ] && return 0

    # Detect Claude Code in the most non-invasive way: ~/.claude/ exists OR
    # the `claude` CLI is on PATH. Both are common.
    if [ -d "$HOME/.claude" ] || has_cmd claude; then
        printf '\n'
        info "${C_BOLD}Claude Code detected.${C_RESET}"
        info "Wire vouch into a project (one-time, per repo):"
        printf '\n'
        printf '    %scd /path/to/your/project%s\n' "$C_BOLD" "$C_RESET"
        printf '    %svouch init%s\n' "$C_BOLD" "$C_RESET"
        printf '    %svouch install-mcp claude-code%s\n' "$C_BOLD" "$C_RESET"
        printf '\n'
        info "Then restart Claude Code — vouch's kb.* tools and slash commands"
        info "(/vouch-recall, /vouch-status, …) will be available."
    fi
}

# --- main ----------------------------------------------------------------

main() {
    info "${C_BOLD}vouch installer${C_RESET}  ${C_DIM}($REPO_URL)${C_RESET}"

    if ! py=$(pick_python); then
        err "no Python >=$MIN_PY_MAJOR.$MIN_PY_MINOR found on PATH."
        err "Install Python first:"
        err "  https://www.python.org/downloads/"
        err "  brew install python@3.12        # macOS"
        err "  sudo apt install python3.12     # Debian/Ubuntu 24.04+"
        exit 1
    fi
    ok "python: $(command -v "$py")"

    ensure_pipx "$py" || exit 1
    install_vouch
    smoke_test || exit 1

    printf '\n'
    info "${C_BOLD}Next:${C_RESET}"
    info "  ${C_BOLD}vouch init${C_RESET}              # create a .vouch/ KB in your project"
    info "  ${C_BOLD}vouch serve${C_RESET}             # start the MCP server"
    info "  ${C_BOLD}vouch --help${C_RESET}            # the rest"
    claude_code_nudge

    printf '\n'
    ok "done. happy reviewing."
}

main
