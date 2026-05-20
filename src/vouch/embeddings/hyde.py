"""HyDE -- Hypothetical Document Embedding expansion."""

from __future__ import annotations

from collections.abc import Callable

DEFAULT_TEMPLATE = (
    "The following is a document that answers the question: '{query}'. "
    "It contains relevant facts, claims, and supporting evidence about {query}."
)


def expand_query_template(query: str, *, min_chars: int = 20) -> str:
    """Pad short queries with HyDE template; pass through long ones."""
    if len(query.strip()) >= min_chars:
        return query
    return DEFAULT_TEMPLATE.format(query=query.strip())


def expand_query_with_llm(query: str, *, llm: Callable[[str], str]) -> str:
    """Use an LLM to draft a hypothetical answer; embed that instead."""
    prompt = (
        "Write a short, factual paragraph that would answer this question. "
        "Stay neutral; don't ask follow-ups.\n\n"
        f"Question: {query}\n\nAnswer:"
    )
    return llm(prompt).strip() or query
