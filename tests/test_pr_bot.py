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
