#!/usr/bin/env bash
set -euo pipefail

# Embeddings suite: reindex_embeddings, dedup_scan, eval_embeddings,
# embeddings_stats. All four live behind the [embeddings] extra. This
# script detects whether that extra is installed and degrades gracefully:
# when it is absent it prints the exact commands you would run instead of
# failing, so the example is always runnable in CI.

VOUCH="${VOUCH:-vouch}"
export VOUCH_AGENT=example-agent

KB="$(mktemp -d)"
trap 'rm -rf "$KB"' EXIT

# Run the probe through the same interpreter that backs $VOUCH (read its
# shebang); fall back to python3 on PATH. This keeps the extra-detection
# honest even when VOUCH points at a venv that PATH does not.
shebang="$(head -1 "$(command -v "$VOUCH" 2>/dev/null || echo "$VOUCH")" 2>/dev/null || true)"
PY="${shebang#\#!}"
[ -x "$PY" ] || PY="python3"

has_embeddings() {
  "$PY" -c 'import sentence_transformers, numpy' >/dev/null 2>&1
}

hr() { printf '\n=== %s ===\n' "$1"; }

hr "init a fresh kb"
"$VOUCH" init --path "$KB" | sed 's/^/  /'
cd "$KB"

hr "register a source so the claims can cite real evidence"
NOTE="$KB/onboarding-doc.md"
printf '%s\n' 'acme-example onboarding notes: deploy + on-call basics.' > "$NOTE"
SRC="$("$VOUCH" source add "$NOTE" --title 'acme-example onboarding doc')"
printf '  source %s\n' "$SRC"

hr "approve a few claims (so there is something to embed and dedup)"
# Two near-identical claims plus a distinct one. dedup_scan should later
# pair the near-identical ones above a high cosine threshold.
p1="$("$VOUCH" propose-claim \
  --text 'acme-example deploys the api service via blue-green rollout.' \
  --source "$SRC" --type observation --confidence 0.9)"
p2="$("$VOUCH" propose-claim \
  --text 'the acme-example api service ships using a blue-green rollout.' \
  --source "$SRC" --type observation --confidence 0.9)"
p3="$("$VOUCH" propose-claim \
  --text 'alice-example owns the on-call rotation for the billing service.' \
  --source "$SRC" --type observation --confidence 0.8)"
# approve as a different actor — vouch forbids self-approval (the review
# gate). the agent proposes; a human reviewer approves.
VOUCH_AGENT=reviewer-example "$VOUCH" approve "$p1" "$p2" "$p3" | sed 's/^/  /'
"$VOUCH" status | sed 's/^/  /'

# Write the labeled query set eval_embeddings scores against. Each row is
# a query plus the ids that count as relevant (recall@k / mrr / ndcg are
# computed over these labels).
QUERIES="$KB/queries.jsonl"
cat > "$QUERIES" <<'JSONL'
{"query": "how is the api service deployed?", "relevant": ["claim::acme"]}
{"query": "who is on call for billing?", "relevant": ["claim::billing"]}
JSONL

if ! has_embeddings; then
  hr "embeddings extra NOT installed — printing intended commands only"
  cat <<EOF
the [embeddings] extra (sentence-transformers + numpy) is not installed in
this environment, so the four vector methods would raise ImportError. install
it with:

    pip install 'vouch[embeddings]'

then the suite runs these four legs. CLI form:

    # 1. reindex_embeddings — backfill vectors for every artifact under the
    #    current model. prints "reindex: embeddings backfilled = N".
    $VOUCH reindex --embeddings

    # 2. dedup_scan — cross-artifact near-duplicates above a cosine threshold.
    #    the two acme-example claims above should pair here.
    $VOUCH dedup --threshold 0.95

    # 3. eval_embeddings — retrieval quality over a labeled JSONL query set.
    $VOUCH eval embedding --queries "$QUERIES" --metric recall@10,mrr,ndcg

    # 4. embeddings_stats — model identity, per-kind counts, cache hit rate.
    $VOUCH embeddings stats

the same four methods over the JSONL tool server ("\$VOUCH" serve --transport jsonl):

    {"id":"r","method":"kb.reindex_embeddings","params":{"force":false}}
    {"id":"d","method":"kb.dedup_scan","params":{"threshold":0.95,"dry_run":false}}
    {"id":"e","method":"kb.eval_embeddings","params":{"queries_path":"$QUERIES","k":10}}
    {"id":"s","method":"kb.embeddings_stats","params":{}}

each leg detects the extra and is expected to be a graceful skip, not a
failure, when the dependency is absent.
EOF
  hr "done (skipped: embeddings extra absent)"
  exit 0
fi

# --- the extra IS present: actually exercise all four legs --------------

hr "1/4 reindex_embeddings — backfill vectors under the current model"
"$VOUCH" reindex --embeddings | sed 's/^/  /'

hr "2/4 dedup_scan — cross-artifact near-duplicates above cosine 0.95"
"$VOUCH" dedup --threshold 0.95 | sed 's/^/  /'

hr "3/4 eval_embeddings — recall@10 / mrr / ndcg over queries.jsonl"
"$VOUCH" eval embedding --queries "$QUERIES" --metric recall@10,mrr,ndcg | sed 's/^/  /'

hr "4/4 embeddings_stats — model identity, per-kind counts, cache hit rate"
"$VOUCH" embeddings stats | sed 's/^/  /'

hr "the same four legs over the JSONL tool server"
printf '%s\n' \
  '{"id":"r","method":"kb.reindex_embeddings","params":{"force":false}}' \
  '{"id":"d","method":"kb.dedup_scan","params":{"threshold":0.95,"dry_run":false}}' \
  "{\"id\":\"e\",\"method\":\"kb.eval_embeddings\",\"params\":{\"queries_path\":\"$QUERIES\",\"k\":10}}" \
  '{"id":"s","method":"kb.embeddings_stats","params":{}}' \
  | "$VOUCH" serve --transport jsonl | sed 's/^/  /'

hr "done (all four legs ran)"
