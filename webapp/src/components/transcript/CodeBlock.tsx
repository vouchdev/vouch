export function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-rule bg-paper">
      {lang && (
        <div className="border-b border-rule px-3 py-1 font-mono text-[10px] uppercase tracking-widest text-sepia">
          {lang}
        </div>
      )}
      <pre className="px-3 py-2 font-mono text-xs text-ink-2">
        <code>{code}</code>
      </pre>
    </div>
  )
}
