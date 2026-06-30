// fileTree.ts — build a nested file tree from a flat list of paths.
// modeled on gittensor-ui's src/components/repositories/fileTree.ts, trimmed to
// what the dual-solve file-changes view needs: no urls, no github tree types.
//
// nodes are folders-first then alphabetical at every level. intermediate
// directories are synthesized from path segments so a lone "a/b/c.py" still
// produces the a → b → c.py chain.

export interface FileNode {
  name: string // last path segment
  path: string // full path from root
  type: 'tree' | 'blob' // directory | file
  children?: FileNode[] // present iff type === 'tree'
}

function sortNodes(nodes: FileNode[]): void {
  nodes.sort((a, b) => {
    if (a.type !== b.type) return a.type === 'tree' ? -1 : 1
    return a.name.localeCompare(b.name)
  })
  for (const n of nodes) if (n.children) sortNodes(n.children)
}

export function buildFileTree(paths: string[]): FileNode[] {
  const roots: FileNode[] = []
  // path -> node, so intermediate dirs are created once and reused.
  const byPath = new Map<string, FileNode>()

  for (const raw of paths) {
    const full = raw.replace(/^\/+|\/+$/g, '')
    if (!full) continue
    const segs = full.split('/')
    let prefix = ''
    let siblings = roots

    segs.forEach((seg, i) => {
      prefix = prefix ? `${prefix}/${seg}` : seg
      const isLeaf = i === segs.length - 1
      let node = byPath.get(prefix)
      if (!node) {
        node = isLeaf
          ? { name: seg, path: prefix, type: 'blob' }
          : { name: seg, path: prefix, type: 'tree', children: [] }
        byPath.set(prefix, node)
        siblings.push(node)
      }
      // descend; only directory nodes carry children.
      if (node.children) siblings = node.children
    })
  }

  sortNodes(roots)
  return roots
}
