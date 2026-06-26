from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LABELER = ROOT / ".github" / "labeler.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "labeler.yml"


def _load_yaml(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_pr_labeler_taxonomy_covers_core_surfaces() -> None:
    labels = _load_yaml(LABELER)
    assert {
        "docs",
        "ci",
        "cli",
        "auto-pr",
        "dual-solve",
        "review-ui",
        "adapters",
        "openclaw",
        "mcp",
        "storage",
        "retrieval",
        "tests",
    }.issubset(labels)

    for label, rules in labels.items():
        assert isinstance(label, str) and label
        assert isinstance(rules, list) and rules, label
        assert any("changed-files" in rule for rule in rules), label


def test_pr_labeler_workflow_is_pull_request_metadata_only() -> None:
    workflow = _load_yaml(WORKFLOW)
    triggers = workflow["on"]
    assert "pull_request_target" in triggers
    assert "workflow_dispatch" in triggers
    assert "pull_request" not in triggers

    jobs = workflow["jobs"]
    assert jobs["label"]["if"] == "github.event_name == 'pull_request_target'"
    assert jobs["backfill-pr-labels"]["if"] == "github.event_name == 'workflow_dispatch'"

    steps = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
    ]
    used_actions = [step.get("uses", "") for step in steps]
    assert any(
        action.startswith("actions/labeler@")
        and not action.endswith("@v6")
        for action in used_actions
    )
    assert "actions/checkout@v4" not in used_actions

    labeler_step = next(
        step for step in jobs["label"]["steps"]
        if step.get("uses", "").startswith("actions/labeler@")
    )
    assert labeler_step["with"]["sync-labels"] is True

    backfill_step = next(
        step for step in jobs["backfill-pr-labels"]["steps"]
        if step.get("uses", "").startswith("actions/labeler@")
    )
    assert backfill_step["with"]["pr-number"] == "${{ steps.open-prs.outputs.result }}"


def test_pr_labeler_workflow_creates_every_configured_label() -> None:
    labels = _load_yaml(LABELER)
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    for label in labels:
        assert f'"{label}"' in workflow_text


def test_pr_labeler_size_labels_follow_openclaw_thresholds() -> None:
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    for label in ["size: XS", "size: S", "size: M", "size: L", "size: XL"]:
        assert f'"{label}"' in workflow_text
    for threshold in ["< 50", "< 200", "< 500", "< 1000"]:
        assert threshold in workflow_text
    for ignored_path in ["docs/", "examples/", "spec/", "package-lock.json"]:
        assert ignored_path in workflow_text
