// Diff.tsx — minimal unified-diff renderer.
// Ported from src/renderer/views/dualsolve.js renderDiff (lines 162-179).
// Splits a unified diff string into per-file sections; colors +/-/context lines.

import type { ReactNode } from 'react'

interface DiffProps {
  diff?: string | null
}

interface DiffLine {
  cls: 'hunk' | 'add' | 'del' | 'ctx'
  text: string
}

interface DiffFile {
  head: string
  lines: DiffLine[]
}

function parseDiff(diff: string): DiffFile[] {
  const files: DiffFile[] = []
  let cur: DiffFile | null = null

  for (const line of diff.split('\n')) {
    if (line.startsWith('diff --git')) {
      const m = line.match(/ b\/(.+)$/)
      cur = { head: m ? m[1] : line, lines: [] }
      files.push(cur)
    } else if (!cur) {
      continue
    } else if (
      line.startsWith('+++') ||
      line.startsWith('---') ||
      line.startsWith('index ')
    ) {
      continue
    } else {
      const cls: DiffLine['cls'] = line.startsWith('@@')
        ? 'hunk'
        : line.startsWith('+')
          ? 'add'
          : line.startsWith('-')
            ? 'del'
            : 'ctx'
      cur.lines.push({ cls, text: line || ' ' })
    }
  }

  return files
}

export function Diff({ diff }: DiffProps): ReactNode {
  const files = parseDiff(diff ?? '')

  if (!files.length) {
    return (
      <div className="diff">
        <p className="muted">(empty diff)</p>
      </div>
    )
  }

  return (
    <div className="diff">
      {files.map((f, i) => (
        <div key={i} className="diff-file">
          <div className="diff-file-head">{f.head}</div>
          {f.lines.map((l, j) => (
            <div key={j} className={`dl ${l.cls}`}>
              {l.text}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
