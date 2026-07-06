# vouch session auto-capture demo — render with:
#   vhs docs/demo.tape
# Produces docs/demo.gif.

Output docs/demo.gif

Require vouch

Set Shell "bash"
Set FontSize 14
Set Width 1240
Set Height 760
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms
Set PlaybackSpeed 1.0

# --- prepare a sandbox project with git history (hidden) -----------------
Hide
Type `D=$(mktemp -d)/acme-api && mkdir -p "$D/src" "$D/tests" && cd "$D"`
Enter
Type `git init -q`
Enter
Type `printf 'def verify(token):\n    return decode(token)\n' > src/auth.py`
Enter
Type `printf 'def test_verify():\n    assert verify("x")\n' > tests/test_auth.py`
Enter
Type `git add -A && git -c user.email=demo@x.com -c user.name=demo commit -q -m "chore: seed"`
Enter
# the "fix" the session makes — left uncommitted so finalize's git-diff backstop catches it
Type `printf 'def verify(token):\n    # reject unsigned tokens\n    return decode(token, verify_signature=True)\n' > src/auth.py`
Enter
Type "clear"
Enter
Show

# --- init ----------------------------------------------------------------
Type "vouch init"
Sleep 300ms
Enter
Sleep 1500ms

# --- a claude code session runs; the PostToolUse hook feeds vouch --------
Type "# a claude code session runs. after each tool, the PostToolUse hook fires:"
Enter
Sleep 900ms
Type `echo '{"session_id":"sess-8f2e4c7a","tool_name":"Edit","tool_input":{"file_path":"src/auth.py"}}' | vouch capture observe`
Enter
Sleep 1300ms

# --- three more tool calls captured off-camera (Read / pytest / Grep) ----
Hide
Type `echo '{"session_id":"sess-8f2e4c7a","tool_name":"Read","tool_input":{"file_path":"src/auth.py"}}' | vouch capture observe`
Enter
Sleep 500ms
Type `echo '{"session_id":"sess-8f2e4c7a","tool_name":"Bash","tool_input":{"command":"pytest -q"}}' | vouch capture observe`
Enter
Sleep 500ms
Type `echo '{"session_id":"sess-8f2e4c7a","tool_name":"Grep","tool_input":{"pattern":"verify_signature"}}' | vouch capture observe`
Enter
Sleep 500ms
Type "clear"
Enter
Show

# --- the gitignored scratch buffer, quietly accumulating -----------------
Type "# vouch logged each to a gitignored scratch buffer — not the KB yet:"
Enter
Sleep 800ms
Type "cat .vouch/captures/sess-8f2e4c7a.jsonl"
Enter
Sleep 2600ms

# --- session ends → SessionEnd hook rolls it into ONE pending summary ----
Type "# session ends → the SessionEnd hook rolls the buffer + git diff into one proposal:"
Enter
Sleep 900ms
Type `echo '{"session_id":"sess-8f2e4c7a"}' | vouch capture finalize`
Enter
Sleep 2400ms

# --- next session start: the nudge, then the review queue ----------------
Type "vouch capture banner"
Enter
Sleep 1700ms
Type "# nothing is auto-approved — you review it like any other write:"
Enter
Sleep 800ms
Type "vouch pending"
Enter
Sleep 2000ms

# --- approve → durable page ----------------------------------------------
Type `PID=$(vouch pending | grep -oE '[0-9]{8}-[0-9]{6}-[0-9a-f]{8}' | head -1)`
Enter
Sleep 400ms
Type "vouch approve $PID --reason 'accurate summary'"
Enter
Sleep 1800ms

# --- the summary vouch kept, now durable markdown on disk ----------------
Type "# the summary it kept — plain markdown, committed alongside your code:"
Enter
Sleep 900ms
Type `sed -n '/^# session summary/,$p' .vouch/pages/session-summary-*.md`
Enter
Sleep 3600ms

# --- the next session starts with it — no amnesia ------------------------
Type "# the next session starts with it injected — no re-explaining:"
Enter
Sleep 900ms
Type "vouch recall"
Enter
Sleep 4000ms
