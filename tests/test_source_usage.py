"""Read-only source-usage report — vouch source usage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import source_usage
from vouch.cli import cli
from vouch.models import Claim, Evidence, Page
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _row(report: source_usage.SourceUsageReport, source_id: str) -> source_usage.SourceUsage:
    for r in report.most_cited + report.orphans:
        if r.id == source_id:
            return r
    raise AssertionError(f"{source_id} not in report")


def test_direct_citation_counts(store: KBStore) -> None:
    src = store.put_source(b"backing material", title="doc")
    store.put_claim(Claim(id="c1", text="a", evidence=[src.id]))
    store.put_claim(Claim(id="c2", text="b", evidence=[src.id]))

    report = source_usage.build(store)
    row = _row(report, src.id)
    assert row.claim_citations == 2
    assert row.is_orphan is False
    assert report.cited_total == 1
    assert report.orphan_total == 0


def test_via_evidence_citation(store: KBStore) -> None:
    src = store.put_source(b"spanned", title="doc")
    store.put_evidence(Evidence(id="ev1", source_id=src.id, locator="L1-L2", quote="q"))
    store.put_claim(Claim(id="c1", text="rests on the span", evidence=["ev1"]))

    report = source_usage.build(store)
    row = _row(report, src.id)
    assert row.claim_citations == 1
    assert row.evidence_spans == 1
    assert row.is_orphan is False


def test_direct_and_via_evidence_dedup(store: KBStore) -> None:
    src = store.put_source(b"both paths", title="doc")
    store.put_evidence(Evidence(id="ev1", source_id=src.id, locator="L1"))
    # one claim naming both the source id and an evidence span into it — the
    # same claim must not be counted twice for the same source.
    store.put_claim(Claim(id="c1", text="cites both ways", evidence=[src.id, "ev1"]))

    report = source_usage.build(store)
    assert _row(report, src.id).claim_citations == 1


def test_page_reference_is_not_orphan(store: KBStore) -> None:
    src = store.put_source(b"page material", title="doc")
    store.put_page(Page(id="p1", title="write-up", sources=[src.id]))

    report = source_usage.build(store)
    # a page reference keeps the source out of the orphan set even though no
    # claim cites it — it is neither cited nor orphaned, a distinct state.
    assert report.sources_total == 1
    assert report.cited_total == 0
    assert report.orphan_total == 0
    assert [r.id for r in report.orphans] == []


def test_orphan_detection(store: KBStore) -> None:
    cited = store.put_source(b"cited", title="cited-doc")
    orphan = store.put_source(b"orphan", title="orphan-doc")
    store.put_claim(Claim(id="c1", text="a", evidence=[cited.id]))

    report = source_usage.build(store)
    assert report.sources_total == 2
    assert report.orphan_total == 1
    assert [r.id for r in report.orphans] == [orphan.id]
    assert _row(report, orphan.id).is_orphan is True


def test_orphan_with_unused_evidence_span_stays_orphan(store: KBStore) -> None:
    # a source can have Evidence spans defined yet be cited by no claim — the
    # spans are informational and do not rescue it from the orphan set.
    orphan = store.put_source(b"has a span nobody cites", title="doc")
    store.put_evidence(Evidence(id="ev1", source_id=orphan.id, locator="L1"))

    report = source_usage.build(store)
    row = _row(report, orphan.id)
    assert row.is_orphan is True
    assert row.evidence_spans == 1
    assert row.claim_citations == 0


def test_dangling_citation_is_ignored(store: KBStore) -> None:
    orphan = store.put_source(b"real orphan", title="doc")
    # a claim citing a source id that isn't on disk — a dangling citation
    # health.lint reports. Written directly to bypass put_claim's existence
    # guard. The report must not crash or invent a row for the missing id.
    (store.kb_dir / "claims" / "dangling.yaml").write_text(
        "id: dangling\n"
        'text: "cites nothing real"\n'
        "type: fact\n"
        "status: working\n"
        "confidence: 0.7\n"
        f"evidence: ['{'a' * 64}']\n",
        encoding="utf-8",
    )

    report = source_usage.build(store)
    assert report.sources_total == 1
    assert _row(report, orphan.id).is_orphan is True


def test_most_cited_ordering_and_limit(store: KBStore) -> None:
    heavy = store.put_source(b"heavy", title="heavy")
    light = store.put_source(b"light", title="light")
    store.put_claim(Claim(id="c1", text="a", evidence=[heavy.id]))
    store.put_claim(Claim(id="c2", text="b", evidence=[heavy.id]))
    store.put_claim(Claim(id="c3", text="c", evidence=[light.id]))

    report = source_usage.build(store, limit=1)
    assert len(report.most_cited) == 1
    assert report.most_cited[0].id == heavy.id
    assert report.most_cited[0].claim_citations == 2


def test_truncation_is_not_silent(store: KBStore) -> None:
    for i in range(3):
        src = store.put_source(f"doc {i}".encode(), title=f"doc-{i}")
        store.put_claim(Claim(id=f"c{i}", text="t", evidence=[src.id]))

    report = source_usage.build(store, limit=1)
    assert report.cited_total == 3
    assert len(report.most_cited) == 1
    assert "... and 2 more cited" in source_usage.render_text(report)
    assert "... and 2 more cited" in source_usage.render_markdown(report)


def test_report_to_dict_schema(store: KBStore) -> None:
    store.put_source(b"x", title="doc")
    body = source_usage.build(store).to_dict()
    assert set(body) == {
        "generated_at",
        "limit",
        "sources_total",
        "cited_total",
        "orphan_total",
        "most_cited",
        "orphans",
    }
    # nested rows carry the stable per-source contract.
    assert set(body["orphans"][0]) == {
        "id",
        "title",
        "type",
        "locator",
        "created_at",
        "claim_citations",
        "page_references",
        "evidence_spans",
        "is_orphan",
    }


def test_empty_kb(store: KBStore) -> None:
    report = source_usage.build(store)
    assert report.sources_total == 0
    assert report.most_cited == []
    assert report.orphans == []
    assert "orphaned sources (0)" in source_usage.render_text(report)


def test_cli_source_usage_json(store: KBStore) -> None:
    src = store.put_source(b"x", title="doc")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))

    result = CliRunner().invoke(cli, ["source", "usage", "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["cited_total"] == 1
    assert data["most_cited"][0]["claim_citations"] == 1


def test_cli_source_usage_text_lists_orphans(store: KBStore) -> None:
    store.put_source(b"orphan", title="orphan-doc")
    result = CliRunner().invoke(cli, ["source", "usage"])
    assert result.exit_code == 0, result.output
    assert "orphaned sources (1)" in result.output
    assert "orphan-doc" in result.output


def test_cli_source_usage_markdown(store: KBStore) -> None:
    src = store.put_source(b"x", title="doc")
    store.put_claim(Claim(id="c1", text="t", evidence=[src.id]))
    result = CliRunner().invoke(cli, ["source", "usage", "--format", "markdown"])
    assert result.exit_code == 0, result.output
    assert "## most cited" in result.output
