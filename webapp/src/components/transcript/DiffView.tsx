export function DiffView({ text }: { text: string }) {
  const lines = text.replace(/\n$/, '').split('\n')
  return (
    <div className="overflow-x-auto rounded-lg border border-rule bg-paper font-mono text-xs">
      {lines.map((line, i) => {
        const cls = line.startsWith('@@')
          ? 'diff-hunk text-accent-2'
          : line.startsWith('+')
            ? 'diff-add bg-ok/10 text-ok'
            : line.startsWith('-')
              ? 'diff-del bg-accent/10 text-accent-2'
              : 'diff-ctx text-ink-2'
        return (
          <div key={i} className={`whitespace-pre px-3 py-0.5 ${cls}`}>
            {line || ' '}
          </div>
        )
      })}
    </div>
  )
}
