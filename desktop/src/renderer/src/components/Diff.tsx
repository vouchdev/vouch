// Diff.tsx — minimal unified-diff renderer.
// Splits a unified diff into per-file sections (via the shared diffParse helper)
// and colors +/-/context lines. The dual-solve file-changes view reuses the
// same parser; this component stays for the all-files stacked rendering.

import type { ReactNode } from 'react'
import { parseDiff } from './diffParse'

interface DiffProps {
  diff?: string | null
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
