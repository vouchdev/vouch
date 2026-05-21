# vouch end-to-end demo — render with:
#   vhs docs/demo.tape
# Produces docs/demo.gif.

Output docs/demo.gif

Require vouch

Set Shell "bash"
Set FontSize 14
Set Width 1100
Set Height 700
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms
Set PlaybackSpeed 1.0

# --- prepare a sandbox -------------------------------------------------
Hide
Type "cd $(mktemp -d) && git init -q && echo 'Authentication uses stateless JWTs signed with RS256.' > auth.md && git add -A && git -c user.email=demo@x.com -c user.name=demo commit -q -m init && clear"
Enter
Sleep 500ms
Show

# --- init -------------------------------------------------------------
Type "vouch init"
Sleep 300ms
Enter
Sleep 1200ms

# --- register a source ------------------------------------------------
Type "SID=$(vouch source add auth.md --title 'auth notes')"
Sleep 300ms
Enter
Sleep 600ms
Type "echo $SID"
Enter
Sleep 1000ms

# --- propose a claim as an agent --------------------------------------
Type "PID=$(VOUCH_AGENT=claude-code vouch propose-claim \"
Enter
Type "  --text 'Authentication uses stateless JWTs signed with RS256' \"
Enter
Type "  --source $SID --type fact --confidence 0.9)"
Enter
Sleep 800ms
Type "echo $PID"
Enter
Sleep 1000ms

# --- review -----------------------------------------------------------
Type "vouch pending"
Enter
Sleep 1500ms

Type "vouch show $PID | head -20"
Enter
Sleep 2500ms

# --- approve ----------------------------------------------------------
Type "vouch approve $PID --reason 'matches the code'"
Enter
Sleep 1500ms

Type "vouch status"
Enter
Sleep 2000ms

# --- retrieve ---------------------------------------------------------
Type 'vouch search "JWT"'
Enter
Sleep 2000ms

Type "vouch audit --tail 4"
Enter
Sleep 3000ms

# --- the durable artifact on disk -------------------------------------
Type "ls .vouch/claims/"
Enter
Sleep 1500ms

Type "cat .vouch/claims/*.yaml"
Enter
Sleep 4000ms
