import { describe, it, expect } from 'vitest'
import { parseDiff } from '../src/renderer/src/components/diffParse'

describe('parseDiff', () => {
  it('splits into per-file sections keyed off the b/ path', () => {
    const d = [
      'diff --git a/src/x.py b/src/x.py',
      'index 111..222 100644',
      '--- a/src/x.py',
      '+++ b/src/x.py',
      '@@ -1,2 +1,2 @@',
      '-old',
      '+new',
    ].join('\n')
    const files = parseDiff(d)
    expect(files).toHaveLength(1)
    expect(files[0].head).toBe('src/x.py')
    // header markers (---, +++, index) are dropped; only hunk + changes remain.
    expect(files[0].lines.map((l) => `${l.cls}:${l.text}`)).toEqual([
      'hunk:@@ -1,2 +1,2 @@',
      'del:-old',
      'add:+new',
    ])
  })

  it('keeps added/removed content lines that start with ++ or --', () => {
    // these begin with +/- twice but are NOT file headers (no trailing space).
    const d = [
      'diff --git a/c.py b/c.py',
      '--- a/c.py',
      '+++ b/c.py',
      '@@ -1,1 +1,2 @@',
      '++counter',
      '---flag',
    ].join('\n')
    const lines = parseDiff(d)[0].lines
    expect(lines).toEqual([
      { cls: 'hunk', text: '@@ -1,1 +1,2 @@' },
      { cls: 'add', text: '++counter' },
      { cls: 'del', text: '---flag' },
    ])
  })

  it('returns [] for an empty diff', () => {
    expect(parseDiff('')).toEqual([])
  })

  it('handles a multi-file diff', () => {
    const d = [
      'diff --git a/a.py b/a.py',
      '@@ -1 +1 @@',
      '+a',
      'diff --git a/b.py b/b.py',
      '@@ -1 +1 @@',
      '+b',
    ].join('\n')
    expect(parseDiff(d).map((f) => f.head)).toEqual(['a.py', 'b.py'])
  })
})
