import { ChevronRight, CornerDownRight, Wrench } from 'lucide-react'
import { useState } from 'react'
import type { TranscriptBlock } from '../../lib/transcript'
import { CodeBlock } from './CodeBlock'
import { DiffView } from './DiffView'

type ToolUse = Extract<TranscriptBlock, { type: 'tool_use' }>

/** One-line summary of the tool's input, agentsview-style. */
function headline(block: ToolUse): string {
  const i = block.input
  const s = (k: string) => (typeof i[k] === 'string' ? (i[k] as string) : '')
  switch (block.name) {
    case 'Bash':
    case 'run_command':
      return s('command') || s('cmd')
    case 'Read':
    case 'Edit':
    case 'MultiEdit':
    case 'Write':
    case 'Update':
    case 'NotebookEdit':
      return s('file_path') || s('path') || s('notebook_path')
    case 'Grep':
      return s('pattern')
    case 'Glob':
      return s('pattern') || s('glob')
    case 'Task':
    case 'Agent':
      return s('subagent_type') || s('description') || s('prompt').slice(0, 80)
    default:
      return ''
  }
}

function ResultBody({ block }: { block: ToolUse }) {
  const r = block.result
  if (!r) return <p className="px-1 text-xs italic text-sepia">no output captured</p>
  const isEdit = ['Edit', 'MultiEdit', 'Write', 'Update'].includes(block.name)
  if (isEdit && /^@@|\n[+-]/.test(r.content)) return <DiffView text={r.content} />
  if (r.is_error) {
    return (
      <pre
        data-testid="tool-error"
        className="overflow-x-auto whitespace-pre-wrap rounded-lg border border-accent/40 bg-accent/10 px-3 py-2 text-xs text-accent-2"
      >
        {r.content}
      </pre>
    )
  }
  return <CodeBlock code={r.content || '(empty)'} />
}

export function ToolBlock({
  block,
  onOpenSubagent,
}: {
  block: ToolUse
  onOpenSubagent?: (sessionId: string) => void
}) {
  const [open, setOpen] = useState(false)
  const head = headline(block)
  const child = block.result?.subagent_session_id ?? null
  return (
    <div className="rounded-lg border border-rule bg-paper-2">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left"
      >
        <ChevronRight size={12} className={`shrink-0 text-sepia transition ${open ? 'rotate-90' : ''}`} />
        <Wrench size={12} className="shrink-0 text-accent" />
        <span className="shrink-0 font-mono text-[11px] font-semibold text-ink">{block.name}</span>
        {head && <span className="truncate font-mono text-[11px] text-sepia">{head}</span>}
        {block.result?.is_error && <span className="ml-auto text-[10px] font-bold text-accent">ERROR</span>}
      </button>
      {open && (
        <div className="space-y-2 px-3 pb-2">
          {Object.keys(block.input).length > 0 && (
            <details className="text-xs">
              <summary className="cursor-pointer font-mono text-[10px] uppercase tracking-widest text-sepia">
                input
              </summary>
              <CodeBlock code={JSON.stringify(block.input, null, 2)} lang="json" />
            </details>
          )}
          <ResultBody block={block} />
          {child && onOpenSubagent && (
            <button
              onClick={() => onOpenSubagent(child)}
              className="flex items-center gap-1.5 rounded-lg border border-accent/40 bg-accent/10 px-2.5 py-1 text-[11px] text-accent-2 transition hover:bg-accent/20"
            >
              <CornerDownRight size={12} /> view subagent
            </button>
          )}
        </div>
      )}
    </div>
  )
}
