import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// Renders KB markdown (page bodies, session summaries) with the console
// theme. react-markdown emits no raw HTML, so untrusted proposal payloads
// stay inert.
export function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown-body min-w-0 text-sm leading-relaxed text-ink-2">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  )
}
