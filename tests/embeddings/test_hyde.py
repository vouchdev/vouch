"""HyDE -- Hypothetical Document Embedding query expansion."""

from __future__ import annotations

from vouch.embeddings.hyde import expand_query_template


def test_template_expansion_adds_context() -> None:
    expanded = expand_query_template("auth")
    assert "auth" in expanded
    assert len(expanded) > len("auth")


def test_template_expansion_idempotent_for_long_queries() -> None:
    long_q = "this is a long descriptive query about something specific"
    out = expand_query_template(long_q, min_chars=20)
    assert out == long_q
