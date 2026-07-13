import { Brain, ChevronRight } from 'lucide-react'
import { useState } from 'react'

export function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-lg border border-rule/60 bg-paper-2">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left font-mono text-[10px] uppercase tracking-widest text-sepia"
      >
        <ChevronRight size={12} className={`transition ${open ? 'rotate-90' : ''}`} />
        <Brain size={12} /> Thinking
      </button>
      {open && <div className="whitespace-pre-wrap px-3 pb-2 text-xs italic text-sepia">{text}</div>}
    </div>
  )
}
