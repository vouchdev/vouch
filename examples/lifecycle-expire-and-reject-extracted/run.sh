#!/usr/bin/env bash
set -euo pipefail

# Garbage-collecting the review queue — expire + reject-extracted.
#
# Two bulk queue-hygiene operations, both with a dry-run-then-apply shape:
#
#   1. `vouch expire` — runs the configured staleness GC over pending
#      proposals. Dry-run first (the would_expire list), then --apply.
#   2. `vouch reject-extracted` — mass-rejects the typed edges the
#      auto-extractor (`vouch-extractor`) files when a page is approved,
#      so a reviewer can clear them in one call instead of one by one.
#
# Mirrors AKBP's runnable examples in shape: fresh KB in a tempdir, real
# CLI output under section headers, cleanup at the end.

VOUCH="${VOUCH:-vouch}"

KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT

# Two distinct actors: the agent that proposes writes, and the reviewer
# that approves them. vouch forbids self-approval, so the reviewer must
# differ from the proposer.
AGENT=example-agent
REVIEWER=example-reviewer

echo "== vouch expire + reject-extracted example =="

# 1. init seeds a starter claim + page; the CLI discovers the KB from cwd.
echo
echo "-- init --"
"$VOUCH" init --path "$KB"
cd "$KB"

# 2. Turn the staleness GC on: a pending proposal older than 7 days is
#    eligible for expiry. (Default is 90; we shrink it for the demo.)
echo
echo "-- set review.expire_pending_after_days: 7 --"
python3 - .vouch/config.yaml <<'PY'
import pathlib, sys, yaml
p = pathlib.Path(sys.argv[1])
cfg = yaml.safe_load(p.read_text()) or {}
cfg.setdefault("review", {})["expire_pending_after_days"] = 7
p.write_text(yaml.safe_dump(cfg, sort_keys=False))
print("review.expire_pending_after_days =", cfg["review"]["expire_pending_after_days"])
PY

# A source to cite (claims must carry evidence that resolves).
printf 'acme-example deploy notes\n' > notes.txt
SRC="$(VOUCH_AGENT=$AGENT "$VOUCH" source add notes.txt --title 'acme-example deploy notes')"

# 3. File two claim proposals. One we leave fresh; the other we backdate
#    so it crosses the 7-day staleness threshold. Editing proposed_at in
#    the proposed/ yaml just simulates time passing — it does not bypass
#    the review gate (the proposal is still pending, still un-approved).
echo
echo "-- file two pending claim proposals --"
FRESH="$(VOUCH_AGENT=$AGENT "$VOUCH" propose-claim \
  --text 'acme-example ships on fridays' --source "$SRC" --confidence 0.6)"
STALE="$(VOUCH_AGENT=$AGENT "$VOUCH" propose-claim \
  --text 'alice-example owns the deploy runbook' --source "$SRC" --confidence 0.6)"
echo "fresh: $FRESH"
echo "stale: $STALE"

echo
echo "-- backdate the stale proposal's proposed_at by 30 days --"
python3 - ".vouch/proposed/${STALE}.yaml" <<'PY'
import datetime, pathlib, sys, yaml
p = pathlib.Path(sys.argv[1])
doc = yaml.safe_load(p.read_text())
old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
doc["proposed_at"] = old.isoformat().replace("+00:00", "Z")
p.write_text(yaml.safe_dump(doc, sort_keys=False))
print("proposed_at ->", doc["proposed_at"])
PY

# 4. expire — dry-run first. Lists what *would* expire; mutates nothing.
echo
echo "-- vouch expire (dry-run) --"
VOUCH_AGENT=$REVIEWER "$VOUCH" expire --json

# 5. expire --apply — actually expires the stale proposal. The fresh one
#    survives. Expiry is a terminal reject recorded in the audit log.
echo
echo "-- vouch expire --apply --"
VOUCH_AGENT=$REVIEWER "$VOUCH" expire --apply

echo
echo "-- pending after expire (fresh claim remains) --"
VOUCH_AGENT=$REVIEWER "$VOUCH" pending

# 6. Now the reject-extracted half. Approve an entity, then approve a page
#    that links to it. Approving the page triggers the auto-extractor,
#    which files typed edge proposals (mentions / relates_to) as
#    `vouch-extractor` — they still need review like any other write.
echo
echo "-- approve an entity, then a page that links to it --"
EP="$(VOUCH_AGENT=$AGENT "$VOUCH" propose-entity --name 'acme-example' --type company)"
VOUCH_AGENT=$REVIEWER "$VOUCH" approve "$EP"
PP="$(VOUCH_AGENT=$AGENT "$VOUCH" propose-page \
  --title 'acme-example deploy' \
  --body 'deploy steps — see [[acme-example]]' \
  --entity 'acme-example')"
VOUCH_AGENT=$REVIEWER "$VOUCH" approve "$PP"
PAGE_ID=acme-example-deploy

echo
echo "-- pending after page approve (vouch-extractor edges queued) --"
VOUCH_AGENT=$REVIEWER "$VOUCH" pending

# 7. reject-extracted — clear all auto-extracted edges from that page in
#    one call. Scoped to vouch-extractor proposals; a hand-filed relation
#    would be left untouched.
echo
echo "-- vouch reject-extracted --page $PAGE_ID --"
VOUCH_AGENT=$REVIEWER "$VOUCH" reject-extracted --page "$PAGE_ID" --reason 'bulk reject'

echo
echo "-- pending after reject-extracted (queue clear) --"
VOUCH_AGENT=$REVIEWER "$VOUCH" pending

# 8. Assert the end state: the audit log carries both the expire and the
#    auto-extracted rejections, and the only thing left pending is the one
#    fresh claim (the stale claim expired, the extractor edges were
#    rejected).
echo
echo "-- assertions --"
python3 - <<'PY'
import json, pathlib, yaml
events = [
    json.loads(line)
    for line in pathlib.Path(".vouch/audit.log.jsonl").read_text().splitlines()
    if line.strip()
]
kinds = [e.get("event") for e in events]
assert "proposal.expire" in kinds, "expected a proposal.expire audit event"
rejects = [e for e in events if e.get("event", "").endswith(".reject")]
assert len(rejects) == 2, f"expected 2 reject events, got {len(rejects)}"
print("audit: proposal.expire present, %d reject event(s) present" % len(rejects))

pending = list(pathlib.Path(".vouch/proposed").glob("*.yaml"))
texts = [yaml.safe_load(p.read_text())["payload"].get("text") for p in pending]
assert texts == ["acme-example ships on fridays"], f"unexpected queue: {texts}"
print("queue: 1 pending proposal — only the fresh claim remains")
PY

echo
echo "== example passed =="
