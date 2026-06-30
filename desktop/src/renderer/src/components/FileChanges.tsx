// FileChanges.tsx — file-changes view for a dual-solve candidate.
// modeled on gittensor-ui's RepositoryCodeBrowser tree→content pattern, trimmed
// to changed files only: a compact nested file tree (folders-first) as a narrow
// left rail drives a content pane showing the selected file's diff hunks.
//
// selection is local state — two instances (claude / codex) are independent by
// design; one candidate's selection never affects the other.

import { useMemo, useState, type ReactNode } from 'react'
import { parseDiff, type DiffFile } from './diffParse'
import { buildFileTree, type FileNode } from './fileTree'

interface FileChangesProps {
  diff?: string | null
}

// recursive rail row: a directory label (non-interactive) or a clickable file.
function TreeRow({
  node,
  depth,
  selected,
  onSelect,
}: {
  node: FileNode
  depth: number
  selected: string | null
  onSelect: (path: string) => void
}): ReactNode {
  const pad = { paddingLeft: `${depth * 10}px` }
  if (node.type === 'tree') {
    return (
      <div className="fc-group">
        <div className="fc-dir" style={pad} title={node.path}>
          {node.name}/
        </div>
        {node.children?.map((c) => (
          <TreeRow
            key={c.path}
            node={c}
            depth={depth + 1}
            selected={selected}
            onSelect={onSelect}
          />
        ))}
      </div>
    )
  }
  const cls = node.path === selected ? 'fc-file sel' : 'fc-file'
  return (
    <button
      type="button"
      className={cls}
      style={pad}
      title={node.path}
      onClick={() => onSelect(node.path)}
    >
      {node.name}
    </button>
  )
}

export function FileChanges({ diff }: FileChangesProps): ReactNode {
  const files: DiffFile[] = useMemo(() => parseDiff(diff ?? ''), [diff])
  const tree = useMemo(() => buildFileTree(files.map((f) => f.head)), [files])

  // default selection = first changed file; falls back if the diff changes.
  const firstPath = files[0]?.head ?? null
  const [selected, setSelected] = useState<string | null>(firstPath)
  const activePath =
    selected && files.some((f) => f.head === selected) ? selected : firstPath
  const active = files.find((f) => f.head === activePath) ?? null

  if (!files.length) {
    return <p className="fc-empty muted">(no file changes)</p>
  }

  return (
    <div className="fc">
      <div className="fc-rail">
        {tree.map((n) => (
          <TreeRow
            key={n.path}
            node={n}
            depth={0}
            selected={activePath}
            onSelect={setSelected}
          />
        ))}
      </div>
      <div className="fc-pane">
        {active && (
          <div className="diff-file">
            <div className="diff-file-head">{active.head}</div>
            {active.lines.map((l, j) => (
              <div key={j} className={`dl ${l.cls}`}>
                {l.text}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
