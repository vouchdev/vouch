export function ErrorCard({ code, message }: { code?: string; message: string }) {
  return (
    <div role="alert" className="rounded-xl border border-accent/40 bg-accent/10 px-4 py-3 text-sm">
      {code && <span className="mr-2 font-mono text-xs text-accent">{code}</span>}
      <span className="text-accent-2">{message}</span>
    </div>
  )
}
