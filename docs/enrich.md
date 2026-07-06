# vouch enrich-page / enrich-pages — thin-page enrichment

A page can land as a stub: a title, a one-line body, and few or no
`claims`/`sources` citations. Those are the pages least useful to a
retrieving agent, and the ones most likely to stay thin because nobody
circles back to fill them in — even though the KB often already holds
approved claims that speak directly to the stub's topic.

`vouch enrich-page` / `vouch enrich-pages` find those thin pages and draft
an enriched revision synthesized *strictly* from related, approved, non-
retracted claims already in the KB (reusing the same machinery as
`kb.synthesize` — see `docs/`) — no external fetch, no invented sentences.
Like `vouch compile`, this only ever proposes: the enriched page is filed
as a pending page proposal a human clears with `vouch review`.

## What counts as thin

A page qualifies when either threshold is under its configured minimum:

```yaml
enrichment:
  min_body_chars: 200   # page.body shorter than this...
  min_citations: 2      # ...or len(page.claims) + len(page.sources) below this
```

Both are overridable per-invocation with `--min-body-chars` / `--min-citations`.

## Run it

```bash
vouch enrich-page <page_id>              # score + draft one page
vouch enrich-page <page_id> --dry-run    # show what would be proposed, file nothing

vouch enrich-pages                       # scan all pages, file one proposal per thin page
vouch enrich-pages --dry-run             # list qualifying pages + candidate claim ids
vouch enrich-pages --limit 10            # cap how many thin pages are processed this run
vouch review                             # the human half: approve / reject each draft
```

## How the addition is built

For a qualifying page, the page's title + body become the query for
`kb.synthesize`, which answers strictly from approved, non-retracted claims
with one cited clause per claim. The result is appended to the page's
existing body (the stub's hand-written text is never discarded or
overwritten) and its cited claim ids are unioned with the page's existing
`claims`. Evidence ids of those claims that resolve to registered sources
are unioned into the page's `sources` list the same way.

A page is skipped (no proposal filed) when:

- it's already above both thresholds,
- no approved claim matches its topic (nothing to cite), or
- the proposal is rejected at the gate (e.g. a page kind's required fields
  tightened since the page was last approved) — this drops only that one
  page rather than sinking a batch `enrich-pages` run.

Proposals are filed by the `page-enricher` actor (or `VOUCH_AGENT` when
set), so under the default gate the reviewing human is always a different
actor and self-approval stays impossible. Each non-dry `enrich-pages` run
appends an `enrich_pages.run` audit event with the filed proposal ids.
