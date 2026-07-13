import { Bot, User } from 'lucide-react'
import type { TranscriptMessage } from '../../lib/transcript'
import { Markdown } from '../Markdown'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolBlock } from './ToolBlock'

export function MessageBlock({
  message,
  onOpenSubagent,
}: {
  message: TranscriptMessage
  onOpenSubagent?: (sessionId: string) => void
}) {
  const isUser = message.role === 'user'
  return (
    <div className="rounded-xl border border-rule bg-paper-2 p-3">
      <div className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-sepia">
        {isUser ? <User size={12} /> : <Bot size={12} className="text-accent" />}
        <span>{isUser ? 'user' : 'assistant'}</span>
        {message.model && <span className="text-ink-2">{message.model}</span>}
      </div>
      <div className="space-y-2">
        {message.blocks.map((b, i) => {
          if (b.type === 'thinking') return <ThinkingBlock key={i} text={b.text} />
          if (b.type === 'tool_use') return <ToolBlock key={i} block={b} onOpenSubagent={onOpenSubagent} />
          return (
            <div key={i} className="markdown-body text-sm text-ink">
              <Markdown>{b.text}</Markdown>
            </div>
          )
        })}
      </div>
    </div>
  )
}
