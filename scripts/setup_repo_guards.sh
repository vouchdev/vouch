#!/usr/bin/env bash
# one-time (idempotent) configuration for the AI auto-merge bot. run with a
# plind-junior owner/admin token: `bash scripts/setup_repo_guards.sh [branch]`.
#
# sets up:
#   - labels: auto-merge, ci: passing, ci: failing
#   - repo setting: allow auto-merge
#   - environment `pr-verify` with the owner as a required reviewer (#2). the
#     verify job pauses for your approval before it runs untrusted head code or
#     touches ANTHROPIC_API_KEY.
#   - a branch RULESET (#1) requiring a PR, code-owner review, and the ci +
#     trust-gate status checks — the versionable replacement for classic branch
#     protection, with org admins allowed to bypass (so you can still merge your
#     own core PRs, which you can't self-approve).
#
# after running, set the api key as a repo secret:
#   gh secret set ANTHROPIC_API_KEY --repo "$REPO"
# the caller passes it explicitly to the pr-verify handler; the environment's
# required-reviewer gate is what protects it — the verify job doesn't run, and
# the key isn't used, until you approve the run.
#
# VERIFY BEFORE RELYING ON IT (schema/ids are account-specific):
#   - the required check names below match .github/workflows/ci.yml job names.
#   - the OrganizationAdmin bypass actor id (1) is correct for this org; adjust
#     `bypass_actors` if you are not an org admin (e.g. RepositoryRole admin).
set -euo pipefail

REPO="${REPO:-vouchdev/vouch}"
BRANCH="${1:-test}"
OWNER_LOGIN="${OWNER_LOGIN:-plind-junior}"
RULESET_NAME="auto-merge guard ($BRANCH)"

echo "==> labels"
gh label create "auto-merge" --repo "$REPO" --color 1d76db --description "owner-authorized: claude code verifies, then auto-merge" --force
gh label create "ci: passing" --repo "$REPO" --color 2ecc71 --description "ci is green" --force
gh label create "ci: failing" --repo "$REPO" --color e74c3c --description "ci is red" --force

echo "==> allow auto-merge"
gh api --method PATCH "repos/$REPO" -F allow_auto_merge=true >/dev/null

echo "==> environment pr-verify (required reviewer: $OWNER_LOGIN)"
owner_id="$(gh api "users/$OWNER_LOGIN" --jq .id)"
env_body="$(mktemp)"
cat > "$env_body" <<JSON
{"reviewers": [{"type": "User", "id": $owner_id}], "deployment_branch_policy": null}
JSON
gh api --method PUT "repos/$REPO/environments/pr-verify" --input "$env_body" >/dev/null
rm -f "$env_body"

echo "==> branch ruleset: $RULESET_NAME"
rules_body="$(mktemp)"
cat > "$rules_body" <<JSON
{
  "name": "$RULESET_NAME",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [{"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}],
  "conditions": {"ref_name": {"include": ["refs/heads/$BRANCH"], "exclude": []}},
  "rules": [
    {"type": "pull_request", "parameters": {"require_code_owner_review": true, "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false, "require_last_push_approval": false, "required_review_thread_resolution": false}},
    {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": true, "required_status_checks": [{"context": "test (py3.11)"}, {"context": "test (py3.12)"}, {"context": "test (py3.13)"}, {"context": "build sdist + wheel"}, {"context": "trust-gate"}]}}
  ]
}
JSON
existing_id="$(gh api "repos/$REPO/rulesets" --jq ".[] | select(.name==\"$RULESET_NAME\") | .id" 2>/dev/null | head -1)"
if [ -n "$existing_id" ]; then
  echo "    updating existing ruleset $existing_id"
  gh api --method PUT "repos/$REPO/rulesets/$existing_id" --input "$rules_body" >/dev/null
else
  echo "    creating new ruleset"
  gh api --method POST "repos/$REPO/rulesets" --input "$rules_body" >/dev/null
fi
rm -f "$rules_body"

echo "done. re-run any time; every step is idempotent."
