export type AnswerSegment = { kind: 'text'; text: string } | { kind: 'citation'; claimId: string }

/** Claim ids are kebab slugs: lowercase alphanumerics and dashes, len >= 3. */
const CITATION = /\[([a-z0-9][a-z0-9-]{1,}[a-z0-9])\]/g

export function parseAnswer(answer: string): AnswerSegment[] {
  const segments: AnswerSegment[] = []
  let last = 0
  for (const m of answer.matchAll(CITATION)) {
    const at = m.index ?? 0
    if (at > last) segments.push({ kind: 'text', text: answer.slice(last, at) })
    segments.push({ kind: 'citation', claimId: m[1] })
    last = at + m[0].length
  }
  if (last < answer.length) segments.push({ kind: 'text', text: answer.slice(last) })
  return segments
}

export type SnippetSegment = { kind: 'plain' | 'match'; text: string }

const HIGHLIGHT = /«([^»]*)»/g

export function parseSnippet(snippet: string): SnippetSegment[] {
  const segments: SnippetSegment[] = []
  let last = 0
  for (const m of snippet.matchAll(HIGHLIGHT)) {
    const at = m.index ?? 0
    if (at > last) segments.push({ kind: 'plain', text: snippet.slice(last, at) })
    segments.push({ kind: 'match', text: m[1] })
    last = at + m[0].length
  }
  if (last < snippet.length) segments.push({ kind: 'plain', text: snippet.slice(last) })
  return segments
}
