import subprocess
import sys
from pathlib import Path

from vouch import pr_bot


def test_core_wins_over_ui():
    assert pr_bot.klass(["src/vouch/server.py", "web/app.js"]) == "core"


def test_ui_paths():
    assert pr_bot.klass(["web/index.html"]) == "ui"
    assert pr_bot.klass(["src/vouch/web/static/x.css"]) == "ui"
    assert pr_bot.klass(["webapp/src/main.tsx"]) == "ui"


def test_code_paths():
    assert pr_bot.klass(["src/vouch/context.py"]) == "code"
    assert pr_bot.klass(["README.md"]) == "code"


def test_core_paths_all_flagged():
    for f in ["src/vouch/proposals.py", "src/vouch/pr_bot.py",
              "src/vouch/migrations/0001_init.py", ".github/workflows/ci.yml",
              "migrations/x.sql"]:
        assert pr_bot.classify([f])["is_core"] is True, f


def test_trust():
    assert pr_bot.is_trusted("OWNER", "plind-junior") is True
    assert pr_bot.is_trusted("CONTRIBUTOR", "rando") is False
    assert pr_bot.is_trusted("NONE", "dependabot[bot]") is True


def test_screenshots_two_gh_images():
    body = (
        "before\n![a](https://user-images.githubusercontent.com/1/a.png)\n"
        "after\n![b](https://github.com/user-attachments/assets/uuid-1234)"
    )
    assert pr_bot.has_before_after_screenshots(body) is True


def test_screenshots_one_image_fails():
    body = "![a](https://user-images.githubusercontent.com/1/a.png)"
    assert pr_bot.has_before_after_screenshots(body) is False


def test_screenshots_external_hosts_dont_count():
    body = "![a](https://example.com/a.png)\n![b](https://example.com/b.png)"
    assert pr_bot.has_before_after_screenshots(body) is False


def test_screenshots_none_body():
    assert pr_bot.has_before_after_screenshots(None) is False


def test_gate_arms_noncore_green_approved():
    assert pr_bot.should_arm_automerge(
        is_core=False, ci_passing=True, claude_verdict="APPROVE", is_draft=False) is True


def test_gate_blocks_core():
    assert pr_bot.should_arm_automerge(
        is_core=True, ci_passing=True, claude_verdict="APPROVE", is_draft=False) is False


def test_gate_blocks_red_ci():
    assert pr_bot.should_arm_automerge(
        is_core=False, ci_passing=False, claude_verdict="APPROVE", is_draft=False) is False


def test_gate_blocks_non_approve():
    assert pr_bot.should_arm_automerge(
        is_core=False, ci_passing=True, claude_verdict="REQUEST_CHANGES", is_draft=False) is False


def test_gate_blocks_draft():
    assert pr_bot.should_arm_automerge(
        is_core=False, ci_passing=True, claude_verdict="APPROVE", is_draft=True) is False


def test_cli_classify_print_klass(tmp_path):
    f = tmp_path / "files.txt"
    f.write_text("web/index.html\n", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-m", "vouch.pr_bot", "classify", "--files-file", str(f), "--print-klass"],
        capture_output=True, text=True, check=True)
    assert out.stdout == "ui"


def test_cli_trust_exit_codes():
    ok = subprocess.run([sys.executable, "-m", "vouch.pr_bot", "trust",
                         "--author-association", "OWNER", "--actor", "plind-junior"])
    bad = subprocess.run([sys.executable, "-m", "vouch.pr_bot", "trust",
                          "--author-association", "NONE", "--actor", "rando"])
    assert ok.returncode == 0 and bad.returncode == 1


def test_extract_changed_paths_plain_file():
    files_json = '[{"filename": "src/vouch/context.py"}]'
    assert pr_bot.extract_changed_paths(files_json) == ["src/vouch/context.py"]


def test_extract_changed_paths_includes_previous_filename_on_rename():
    files_json = (
        '[{"filename": "src/vouch/web_server.py", "status": "renamed", '
        '"previous_filename": "src/vouch/http_server.py"}]'
    )
    assert pr_bot.extract_changed_paths(files_json) == [
        "src/vouch/web_server.py", "src/vouch/http_server.py",
    ]


def test_rename_of_core_path_still_classifies_core():
    # a rename that lands a core path under a new name must not slip past
    # trust-gate — the pre-rename path has to stay in the classified list.
    files_json = (
        '[{"filename": "src/vouch/web_server.py", "status": "renamed", '
        '"previous_filename": "src/vouch/http_server.py"}]'
    )
    changed = pr_bot.extract_changed_paths(files_json)
    assert pr_bot.classify(changed)["is_core"] is True


def test_cli_changed_files_emits_previous_filename(tmp_path):
    f = tmp_path / "files.json"
    f.write_text(
        '[{"filename": "src/vouch/web_server.py", "status": "renamed", '
        '"previous_filename": "src/vouch/http_server.py"}]',
        encoding="utf-8",
    )
    out = subprocess.run(
        [sys.executable, "-m", "vouch.pr_bot", "changed-files", "--json-file", str(f)],
        capture_output=True, text=True, check=True)
    assert out.stdout.splitlines() == [
        "src/vouch/web_server.py", "src/vouch/http_server.py",
    ]


def test_codeowners_covers_every_core_glob():
    text = Path(".github/CODEOWNERS").read_text(encoding="utf-8")
    for glob in pr_bot.CORE_GLOBS:
        needle = "/" + glob.replace("/**", "/")
        assert needle in text, f"{glob} missing from .github/CODEOWNERS"


def _review(state, sha, login="coderabbitai[bot]"):
    return {"user": {"login": login}, "state": state, "commit_id": sha}


def test_coderabbit_pending_when_no_review_on_head():
    # approved an earlier commit, but head has no coderabbit review yet.
    reviews = [_review("APPROVED", "old")]
    assert pr_bot.coderabbit_verdict(reviews, head_sha="new") == ("pending", 0)


def test_coderabbit_approved_on_head():
    reviews = [_review("CHANGES_REQUESTED", "c1"), _review("APPROVED", "c2")]
    assert pr_bot.coderabbit_verdict(reviews, head_sha="c2") == ("approved", 1)


def test_coderabbit_changes_on_head():
    reviews = [_review("CHANGES_REQUESTED", "c1")]
    assert pr_bot.coderabbit_verdict(reviews, head_sha="c1") == ("changes", 1)


def test_coderabbit_strikes_count_distinct_commits():
    reviews = [
        _review("CHANGES_REQUESTED", "c1"),
        _review("CHANGES_REQUESTED", "c1"),  # same commit, still one strike
        _review("CHANGES_REQUESTED", "c2"),
        _review("CHANGES_REQUESTED", "c3"),
    ]
    verdict, strikes = pr_bot.coderabbit_verdict(reviews, head_sha="c3")
    assert (verdict, strikes) == ("changes", 3)


def test_coderabbit_ignores_other_reviewers_and_comments():
    reviews = [
        _review("APPROVED", "c1", login="rando"),      # not coderabbit
        _review("COMMENTED", "c1"),                     # no verdict
        _review("CHANGES_REQUESTED", "c1"),
    ]
    assert pr_bot.coderabbit_verdict(reviews, head_sha="c1") == ("changes", 1)


def test_gate_status_maps_verdicts():
    assert pr_bot.gate_status("approved") == "success"
    assert pr_bot.gate_status("changes") == "failure"
    assert pr_bot.gate_status("pending") == "pending"


def test_should_close_after_three_strikes():
    assert pr_bot.should_close("changes", 3, author="rando") is True
    assert pr_bot.should_close("changes", 2, author="rando") is False


def test_should_close_never_when_approved():
    assert pr_bot.should_close("approved", 5, author="rando") is False


def test_should_close_exempts_owner_and_bots():
    assert pr_bot.should_close("changes", 9, author="plind-junior") is False
    assert pr_bot.should_close("changes", 9, author="dependabot[bot]") is False


def test_cli_coderabbit_gate_outputs(tmp_path):
    import json as _json
    f = tmp_path / "reviews.json"
    f.write_text(_json.dumps([
        _review("CHANGES_REQUESTED", "c1"),
        _review("CHANGES_REQUESTED", "c2"),
        _review("CHANGES_REQUESTED", "head"),
    ]), encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-m", "vouch.pr_bot", "coderabbit-gate",
         "--reviews-file", str(f), "--head-sha", "head", "--author", "rando"],
        capture_output=True, text=True, check=True)
    assert "state=failure" in out.stdout
    assert "verdict=changes" in out.stdout
    assert "strikes=3" in out.stdout
    assert "close=true" in out.stdout
