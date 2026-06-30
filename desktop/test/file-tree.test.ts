import { describe, it, expect } from 'vitest'
import { buildFileTree, type FileNode } from '../src/renderer/src/components/fileTree'

// flatten the tree to "<type>:<path>" in render order, for compact assertions.
function flat(nodes: FileNode[], out: string[] = []): string[] {
  for (const n of nodes) {
    out.push(`${n.type}:${n.path}`)
    if (n.children) flat(n.children, out)
  }
  return out
}

describe('buildFileTree', () => {
  it('returns [] for empty input', () => {
    expect(buildFileTree([])).toEqual([])
  })

  it('keeps a single root-level file flat', () => {
    expect(buildFileTree(['README.md'])).toEqual([
      { name: 'README.md', path: 'README.md', type: 'blob' },
    ])
  })

  it('synthesizes intermediate directories from path segments', () => {
    const t = buildFileTree(['src/parser.py'])
    expect(flat(t)).toEqual(['tree:src', 'blob:src/parser.py'])
    expect(t[0].type).toBe('tree')
    expect(t[0].children?.[0]).toMatchObject({ name: 'parser.py', type: 'blob' })
  })

  it('sorts folders first, then files, alphabetically at every level', () => {
    const t = buildFileTree([
      'zeta.txt',
      'src/parser.py',
      'alpha.txt',
      'src/aaa.py',
      'docs/guide.md',
    ])
    // dirs (docs, src) before files (alpha, zeta); each alpha-sorted.
    expect(flat(t)).toEqual([
      'tree:docs',
      'blob:docs/guide.md',
      'tree:src',
      'blob:src/aaa.py',
      'blob:src/parser.py',
      'blob:alpha.txt',
      'blob:zeta.txt',
    ])
  })

  it('merges files that share a directory under one node', () => {
    const t = buildFileTree(['src/a.py', 'src/b.py'])
    expect(t).toHaveLength(1)
    expect(t[0]).toMatchObject({ path: 'src', type: 'tree' })
    expect(t[0].children).toHaveLength(2)
  })

  it('de-dupes a repeated path', () => {
    expect(buildFileTree(['x.py', 'x.py'])).toHaveLength(1)
  })

  it('handles deep nesting', () => {
    const t = buildFileTree(['a/b/c/d.py'])
    expect(flat(t)).toEqual(['tree:a', 'tree:a/b', 'tree:a/b/c', 'blob:a/b/c/d.py'])
  })
})
