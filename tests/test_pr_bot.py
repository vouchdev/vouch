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


def test_codeowners_covers_every_core_glob():
    text = Path(".github/CODEOWNERS").read_text(encoding="utf-8")
    for glob in pr_bot.CORE_GLOBS:
        needle = "/" + glob.replace("/**", "/")
        assert needle in text, f"{glob} missing from .github/CODEOWNERS"
