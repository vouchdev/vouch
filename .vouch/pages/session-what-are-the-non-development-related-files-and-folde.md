---
id: session-what-are-the-non-development-related-files-and-folde
title: 'session: what are the non-development related files and folders in this… [vouch]'
type: session
status: draft
claims: []
entities: []
sources: []
tags: []
metadata: {}
created_at: '2026-07-07T09:30:42.654973Z'
updated_at: '2026-07-07T09:30:42.654976Z'
---
# session: what are the non-development related files and folders in this… [vouch]

- generated: 2026-07-07T09:30:05.593327+00:00
- session: `e5ead202-24c2-4916-b59c-ddb4c123d63f`
- observations: 11

## prompt

> what are the non-development related files and folders in this project? list down them

## files modified this session

- .gitignore
- .vouch/.gitignore
- .vouch/audit.log.jsonl
- .vouch/claims/vouch-uses-a-review-gated-proposal-workflow-agents-propose-c.yaml
- .vouch/config.yaml
- .vouch/decided/20260521-055206-7d6d92d6.yaml
- .vouch/sources/06d8519f8dcf4149d23c8a48984541b2e9365ec364e7e58192e28ed149a2c47c/content
- .vouch/sources/06d8519f8dcf4149d23c8a48984541b2e9365ec364e7e58192e28ed149a2c47c/meta.yaml
- .vouch/sources/67478e72acfb8fac3a059143e95c95f5cc6f7e8d4dccc05fbcea8dbccb8a4eba/content
- .vouch/sources/67478e72acfb8fac3a059143e95c95f5cc6f7e8d4dccc05fbcea8dbccb8a4eba/meta.yaml
- /home/a/.claude/projects/-home-a-Dev-plind-junior-vouch/memory/MEMORY.md
- /home/a/.claude/projects/-home-a-Dev-plind-junior-vouch/memory/commit-scope-src-adapters.md
- /tmp/claude-1000/-home-a-Dev-plind-junior-vouch/e5ead202-24c2-4916-b59c-ddb4c123d63f/scratchpad/merge_hook.py

## git changes

```
.gitignore                                         |   2 +
 .vouch/.gitignore                                  |   1 +
 .vouch/audit.log.jsonl                             |  11 +-
 ...w-gated-proposal-workflow-agents-propose-c.yaml |  22 --
 .vouch/config.yaml                                 |  20 +-
 .vouch/decided/20260521-055206-7d6d92d6.yaml       |  25 ---
 .../content                                        |   6 -
 .../meta.yaml                                      |  12 --
 .../content                                        | 236 ---------------------
 .../meta.yaml                                      |  12 --
 10 files changed, 22 insertions(+), 325 deletions(-)
```

## activity

- Bash: 7
- Edit: 1
- Read: 1
- Write: 2

## notable commands

- `ls -la`
- `git ls-files | sed 's|/.*||' | sort -u`
- `ls -la .claude/ 2>/dev/null; echo "---- settings.json ----"; cat .claude/settings.json 2>/dev/null || echo "(none)"; echo "---- settings.local.json ----"; cat .claude/settings.local.json 2>/dev/null |`
- `git ls-files .claude/ | head; echo "--- check-ignore ---"; git check-ignore .claude/settings.local.json .claude/settings.json 2>/dev/null; echo "exit:$?"; echo "--- status of .claude ---"; git status `
- `echo "=== test 1: mixed list (should ask, listing only non-src/adapters) ==="
outside=$(printf 'src/vouch/foo.py\nadapters/claude-code/x.md\nREADME.md\n.vouch/config.yaml\ndocs/a.md\n' | grep -vE '^(s`
- `python3 /tmp/claude-1000/-home-a-Dev-plind-junior-vouch/e5ead202-24c2-4916-b59c-ddb4c123d63f/scratchpad/merge_hook.py`
- `cd /home/a/Dev/plind-junior/vouch

echo "=== schema validation (jq -e finds the command) ==="
jq -e '.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[] | select(.["if"]=="Bash(git commit*)") | `

## observations

- Ran: ls -la
- Ran: git ls-files | sed 's|/.*||' | sort -u
- Ran: ls -la .claude/ 2>/dev/null; echo "---- settings.json ----";
- Ran: git ls-files .claude/ | head; echo "--- check-ignore ---"; g
- Ran: echo "=== test 1: mixed list (should ask, listing only non-s
- Created merge_hook.py
- Ran: python3 /tmp/claude-1000/-home-a-Dev-plind-junior-vouch/e5ea
- Ran: cd /home/a/Dev/plind-junior/vouch
- Created commit-scope-src-adapters.md
- Read MEMORY.md
- Edited MEMORY.md
