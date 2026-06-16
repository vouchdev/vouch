---
name: pr-precheck
description: Use when the user invokes `/pr-precheck <repo> <topic>` or — implicitly, before any other skill opens a pull request — to check the target repo's merged and closed PR history for prior attempts at the same fix, so the agent doesn't open the Nth duplicate of an already-rejected approach. Wraps the `vouch pr-cache` CLI (build / check / show) and turns its verdict into a clear stop / ask / proceed signal.
---

# PR Precheck

**Goal:** before you raise a PR, find out whether someone already tried this fix — especially whether anyone tried and got *rejected*. The cost of asking is seconds; the cost of a duplicate PR is reviewer attention, contributor reputation, and (on Bittensor-style mining repos) wasted scoring weight.

This skill is a thin orchestration layer over the `vouch pr-cache` CLI shipped in vouch ≥ this PR. It's safe to invoke directly **and** to chain ahead of `pr-fix` / `prs-auto`.

## Invocation

Direct:

```
/pr-precheck <repo> <topic-summary>
/pr-precheck <repo> <topic-summary> --files a.py,b.py
```

Implicit (auto-fires inside `pr-fix` / `prs-auto` before the "open PR" step):

```
/pr-fix https://github.com/owner/repo/issues/N
   └─ before pushing the branch, run pr-precheck on the planned PR title + files
```

`<repo>` may be:

- `https://github.com/<owner>/<name>`
- `git@github.com:<owner>/<name>.git`
- `<owner>/<name>` (shorthand)

`<topic-summary>` is the title-like phrase that describes the PR you're about to raise. Be specific — short generic strings ("fix bug") will match too many cached PRs.

## Prerequisites

- **`vouch` CLI** on `PATH` (`pip install vouch-kb` ≥ the version that ships `pr-cache`).
- **`gh` CLI** authenticated for the target repo's read scope (`gh auth status` returns a session).
- *Optional:* `claude` CLI on `PATH` **or** `ANTHROPIC_API_KEY` in env — only needed if you pass `--analyze-closed` and want LLM-summarised "why was this closed" notes attached to each closed PR record.

If `vouch` is missing, **stop and tell the user**. Don't fall back to manual `gh` queries — the whole value of this skill is the cached, ranked, dedup-aware view.

## Step 1 — Ensure cache is present and fresh

The cache lives at `~/.cache/vouch/pr-cache/<owner>__<name>.json`. Build it if missing, or if it's older than 24 h, or if the user passes `--rebuild`.

```bash
CACHE=~/.cache/vouch/pr-cache/${OWNER}__${REPO}.json
if [ ! -f "$CACHE" ] || [ "$(find "$CACHE" -mmin +1440 -print 2>/dev/null)" ]; then
  vouch pr-cache build "$REPO_URL" --state all --limit 200
fi
```

For repos with > 200 PRs per state, bump `--limit` (the cap is gh API rate limit, not vouch). If the target repo has a known cluster of duplicate-prone areas (e.g. Bittensor mining repos), prefer `--analyze-closed` once so closed-PR rejection reasons are cached for future runs:

```bash
vouch pr-cache build "$REPO_URL" --analyze-closed
```

This calls the local `claude` CLI per closed PR (no API key needed) — slow on the first run, cheap on every subsequent `check` because the analysis is persisted.

## Step 2 — Run the check

```bash
vouch pr-cache check "$REPO" \
  --topic "$TOPIC" \
  ${FILES:+--files "$FILES"} \
  --top-k 5
```

Output is JSON with shape:

```json
{
  "verdict": "likely_duplicate" | "review_candidates" | "no_match",
  "cache_size": 335,
  "candidates": [
    {
      "number": 1419,
      "state": "closed",
      "title": "...",
      "url": "...",
      "score": 0.6,
      "title_overlap": 0.6,
      "path_overlap": 0.0,
      "close_analysis": { "reason": "...", "do_not_repeat": [...] } | null
    }
  ]
}
```

Score is 0..1 — overlap coefficient on title+body tokens, optionally blended with file-path Jaccard when `--files` is passed. Closed-not-merged PRs outrank merged ones on score ties (the "this was rejected before" signal is the higher-value one).

## Step 3 — Branch on the verdict

| Verdict | Score range | What you do |
|---------|-------------|-------------|
| `likely_duplicate` | top score ≥ 0.70 | **Stop.** Surface the top 3 candidates to the user with their titles, states, URLs, and any cached `close_analysis.reason` / `do_not_repeat` bullets. Ask: "Open anyway, fold this into one of those, or skip?" |
| `review_candidates` | any candidate ≥ 0.15, top < 0.70 | **Pause.** Present the top 3. Most often these are *related* PRs, not exact duplicates. Read the closed ones' titles + comments; if any of them tried exactly your approach and got rejected, that's a strong signal to change tactics. Ask the user to confirm before proceeding. |
| `no_match` | no candidate ≥ 0.15 | **Proceed.** Log the cache size and the topic so the trail is auditable, then carry on with the originating workflow. |

## Step 4 — Report to the user

Always print, even on `no_match`, so the trail is visible:

```
PR Precheck (entrius/gittensor)
  Topic:        "normalize github_id to str for miner matching"
  Cache:        335 PRs (built 14m ago)
  Verdict:      review_candidates
  Top matches:
    1. #1419 [closed] (0.60)  fix(classes): normalize MinerEvaluation github_id to str
    2. #1417 [closed] (0.60)  Normalize GitHub IDs for miner matching
    3. #1416 [closed] (0.60)  fix: normalize github_id to str, prevent false cancel votes
  Recommendation: read #1419 and #1417 reviewer comments before raising — same
                  approach was rejected twice in the last 30 days.
```

When invoked **implicitly** by another skill, return the verdict + summary as structured data instead of pretty-printing — the parent skill decides how to surface it. A simple JSON envelope works:

```json
{
  "verdict": "review_candidates",
  "should_proceed": false,
  "human_summary": "<the text above>",
  "candidates": [ ... ]
}
```

## Failure modes

| Symptom | Cause | Action |
|---------|-------|--------|
| `vouch: command not found` | vouch not installed | Tell the user. Do not fall back to manual gh queries. |
| `Error: ... gh ... not found` | gh CLI missing | Tell the user. |
| `Error: gh exited 4: HTTP 401` | gh not authenticated for that repo | Run `gh auth status`; ask the user to authenticate. |
| `verdict: no_match` on a repo you just contributed to | Cache stale (last build pre-dates your other PRs) | Rerun with `--rebuild`. |
| Many false positives at score 0.50–0.65 | Topic is too generic ("fix bug", "refactor") | Re-ask with a sharper topic that uses domain-specific nouns (filenames, class names, error strings). |

## Why this exists

Validated against `entrius/gittensor` (Bittensor SN74 / Gittensor): one `vouch pr-cache check` against the topic `"normalize github_id to str for miner matching"` surfaced **four** closed-not-merged PRs (#1414, #1416, #1417, #1419), all attempting the same fix, all rejected. Without this skill, the fifth contributor would have opened the fifth duplicate. With it, that contributor sees the wreckage in seconds and decides whether to:

1. Read the rejection comments and pick a different approach.
2. Pick up one of the existing closed PRs and address its review.
3. Confirm the prior PRs are genuinely unrelated and proceed.

All three of those are wins; opening duplicate #5 is the only loss.

## Integration hook for `pr-fix` / `prs-auto`

When this skill is invoked as a sub-step (not directly by the user), it should:

1. **Not** ask the user before running — it's part of the parent workflow.
2. Run with the parent's planned PR title as `--topic` and the planned changed files as `--files`.
3. Return its verdict; the parent decides whether to halt + escalate to the user or proceed silently.
4. On `likely_duplicate`, the parent **must** stop and surface, even if the user originally said "just go".

A simple parent-side gate:

```bash
PRECHECK=$(claude run /pr-precheck "$REPO" "$TITLE" --files "$FILES" --as-data)
case "$(echo "$PRECHECK" | jq -r .verdict)" in
  likely_duplicate)  echo "$PRECHECK" | jq -r .human_summary; exit 0 ;;
  review_candidates) echo "$PRECHECK" | jq -r .human_summary; read -p "proceed? " yn; [ "$yn" = y ] || exit 0 ;;
  no_match)          : ;;  # proceed
esac
```
