"""Mechanical coding-content classifier for passive answer-memory."""

from __future__ import annotations

from vouch.capture_filters import is_coding_answer


def test_pure_code_block_is_coding() -> None:
    answer = (
        "```python\n"
        "def fib(n):\n"
        "    a, b = 0, 1\n"
        "    for _ in range(n):\n"
        "        a, b = b, a + b\n"
        "    return a\n"
        "```"
    )
    assert is_coding_answer("write fib", answer) is True


def test_unified_diff_is_coding() -> None:
    answer = (
        "diff --git a/x.py b/x.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-old = 1\n"
        "+new = 2\n"
        " unchanged\n"
    )
    assert is_coding_answer("apply patch", answer) is True


def test_shell_transcript_is_coding() -> None:
    answer = (
        "$ pip install vouch-kb\n"
        "Successfully installed vouch-kb-1.0.0\n"
        "$ vouch init\n"
        "Initialized empty kb\n"
    )
    assert is_coding_answer("how to install", answer) is True


def test_decision_about_code_is_kept() -> None:
    answer = (
        "We chose an append-only jsonl log instead of sqlite because "
        "plaintext diffs in pull requests are the whole point.\n"
        "```python\nlog.append(event)\n```"
    )
    assert is_coding_answer("why jsonl?", answer) is False


def test_code_with_surrounding_rationale_is_kept() -> None:
    answer = (
        "The retry wrapper matters most when the network is flaky and a "
        "single dropped packet would otherwise fail the whole ingest. It "
        "keeps the fetch loop resilient without changing call sites, which "
        "is how every fetch path funnels through one helper today.\n"
        "```python\nfetch(url)\n```"
    )
    assert is_coding_answer("explain retries", answer) is False


def test_plain_prose_is_kept() -> None:
    answer = (
        "Vouch is a knowledge base where every write goes through a review "
        "gate. That invariant is the whole design; files on disk and the "
        "audit log are downstream of it."
    )
    assert is_coding_answer("what is vouch", answer) is False


def test_empty_answer_is_kept() -> None:
    assert is_coding_answer("", "") is False
    assert is_coding_answer("q", "   \n  ") is False
