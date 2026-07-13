import { expect, test } from 'vitest'
import { parseAnswer, parseSnippet } from './citations'

test('parses a real synthesize answer into text + citation segments', () => {
  const answer =
    'The vouch HTTP server binds 127.0.0.1:8731 by default [the-vouch-http-server-binds-127-0-0-1-8731-by-default]. Vouch stores reviewed knowledge [vouch-starter-reviewed-knowledge].'
  const segs = parseAnswer(answer)
  expect(segs).toEqual([
    { kind: 'text', text: 'The vouch HTTP server binds 127.0.0.1:8731 by default ' },
    { kind: 'citation', claimId: 'the-vouch-http-server-binds-127-0-0-1-8731-by-default' },
    { kind: 'text', text: '. Vouch stores reviewed knowledge ' },
    { kind: 'citation', claimId: 'vouch-starter-reviewed-knowledge' },
    { kind: 'text', text: '.' },
  ])
})

test('answer with no citations is a single text segment', () => {
  expect(parseAnswer('nothing cited here')).toEqual([{ kind: 'text', text: 'nothing cited here' }])
})

test('adjacent citations produce no empty text segments', () => {
  const segs = parseAnswer('[claim-a][claim-b]')
  expect(segs).toEqual([
    { kind: 'citation', claimId: 'claim-a' },
    { kind: 'citation', claimId: 'claim-b' },
  ])
})

test('bracketed text that is not a slug stays text', () => {
  // uppercase / spaces / underscores are not claim slugs
  expect(parseAnswer('see [NOT A SLUG] ok')).toEqual([{ kind: 'text', text: 'see [NOT A SLUG] ok' }])
})

test('empty answer parses to empty list', () => {
  expect(parseAnswer('')).toEqual([])
})

test('parseSnippet splits guillemet highlights', () => {
  expect(parseSnippet('The vouch «HTTP» «server» binds')).toEqual([
    { kind: 'plain', text: 'The vouch ' },
    { kind: 'match', text: 'HTTP' },
    { kind: 'plain', text: ' ' },
    { kind: 'match', text: 'server' },
    { kind: 'plain', text: ' binds' },
  ])
})

test('parseSnippet without highlights returns one plain segment', () => {
  expect(parseSnippet('plain text')).toEqual([{ kind: 'plain', text: 'plain text' }])
})
