import { Bot, ChevronDown, Star, User } from 'lucide-react'
import { useState } from 'react'
import type { TranscriptMessage } from '../../lib/transcript'
import { Markdown } from '../Markdown'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolBlock } from './ToolBlock'

export function MessageBlock({
  message,
  dialog = false,
  showHidden = false,
  onOpenSubagent,
}: {
  message: TranscriptMessage
  /** Dialog view: render only the visible conversation text — no tools,
   * thinking, or injected blocks (the working steps live behind stubs). */
  dialog?: boolean
  showHidden?: boolean
  onOpenSubagent?: (sessionId: string) => void
}) {
  const isUser = message.role === 'user'
  const relevance = message.relevance
  // Low-relevance dialog collapses to its header until the reviewer asks.
  const [lowOpen, setLowOpen] = useState(false)
  const [revealed, setRevealed] = useState<Set<number>>(new Set())
  const collapsedLow = relevance?.grade === 'low' && !lowOpen

  return (
    <div
      className={`rounded-xl border border-rule bg-paper-2 p-3 ${
        relevance?.grade === 'low' ? 'opacity-60' : ''
      } ${relevance?.grade === 'key' ? 'border-accent/50' : ''}`}
    >
      <div className="mb-2 flex flex-wrap items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-sepia">
        {isUser ? <User size={12} /> : <Bot size={12} className="text-accent" />}
        <span>{isUser ? 'user' : 'assistant'}</span>
        {message.model && <span className="text-ink-2">{message.model}</span>}
        {relevance?.grade === 'key' && (
          <span className="flex items-center gap-1 rounded bg-accent/15 px-1.5 py-0.5 normal-case tracking-normal text-accent-2">
            <Star size={10} /> key moment
            {relevance.note && <span className="text-sepia">— {relevance.note}</span>}
          </span>
        )}
        {relevance?.grade === 'low' && (
          <button
            onClick={() => setLowOpen((v) => !v)}
            className="flex items-center gap-1 rounded bg-paper px-1.5 py-0.5 normal-case tracking-normal text-sepia hover:text-ink"
          >
            <ChevronDown
              size={10}
              className={collapsedLow ? '-rotate-90 transition-transform' : 'transition-transform'}
            />
            low relevance {collapsedLow ? '— show' : ''}
          </button>
        )}
      </div>
      {!collapsedLow && (
        <div className="space-y-2">
          {message.blocks.map((b, i) => {
            if (b.type === 'thinking')
              return dialog ? null : <ThinkingBlock key={i} text={b.text} />
            if (b.type === 'tool_use')
              return dialog ? null : (
                <ToolBlock key={i} block={b} onOpenSubagent={onOpenSubagent} />
              )
            if (b.noise && dialog) return null
            if (b.noise && !showHidden && !revealed.has(i)) {
              return (
                <button
                  key={i}
                  onClick={() => setRevealed((s) => new Set(s).add(i))}
                  className="block w-full rounded-md border border-dashed border-rule/70 px-2 py-1 text-left font-mono text-[11px] text-sepia/80 hover:bg-paper"
                >
                  ▸ {b.noise} hidden
                </button>
              )
            }
            return (
              <div
                key={i}
                className={`markdown-body text-sm text-ink ${b.noise ? 'opacity-60' : ''}`}
              >
                <Markdown>{b.text}</Markdown>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
