// blurbs.ts — viewBlurb map.
// Faithful port of viewBlurb() in src/renderer/app.js:135-149.

export const VIEW_BLURBS: Record<string, string> = {
  dashboard: 'health, counts, and review throughput for this knowledge base.',
  search: 'retrieve context and synthesize cited answers from approved claims.',
  browse: 'list and open claims, pages, entities, relations, and sources.',
  propose: 'everything you create here enters the review queue — nothing is durable until approved.',
  review: 'approve, reject, and run lifecycle operations. this is the gate.',
  sessions: 'start a working session, volunteer context, and crystallize learnings.',
  graph: 'neighbors, provenance (why / trace / impact), and graph export.',
  maintenance: 'index, lint, doctor, embeddings, and integrity checks.',
  'export-import': 'move a knowledge base between repos as a verifiable bundle.',
  audit: 'the authoritative review-decision timeline.',
  'dual-solve': 'run claude + codex on one issue, compare diffs, pick a winner — proposed to the kb.',
}

export function viewBlurb(id: string): string {
  return VIEW_BLURBS[id] ?? ''
}
