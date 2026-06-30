// JsonTree.tsx — recursive JSON-tree fallback renderer.
//
// Ported from src/renderer/lib/result-render.js:244-257 (jsonTree).
// Faithfully reproduces leaf / array / object branches; open at root (key == null).

// ---------------------------------------------------------------------------
// JsonTree
// ---------------------------------------------------------------------------
interface JsonTreeProps {
  value: unknown
  k?: string | number | null
}

export function JsonTree({ value, k = null }: JsonTreeProps): JSX.Element {
  // Leaf branch: null or non-object
  if (value === null || typeof value !== 'object') {
    return (
      <div className="jt-leaf">
        {k != null && <span className="jt-key">{k + ': '}</span>}
        <span className={`jt-val jt-${typeof value}`}>
          {typeof value === 'string' ? value : JSON.stringify(value)}
        </span>
      </div>
    )
  }

  // Node branch: array or object
  const entries: [string | number, unknown][] = Array.isArray(value)
    ? value.map((v, i) => [i, v])
    : Object.entries(value)

  const summary = Array.isArray(value)
    ? `[${entries.length}]`
    : `{${entries.length}}`

  return (
    <details className="jt-node" {...(k == null ? { open: true } : {})}>
      <summary>
        {k != null ? `${k} ` : ''}
        <span className="muted">{summary}</span>
      </summary>
      <div className="jt-body">
        {entries.map(([ek, ev]) => (
          <JsonTree key={String(ek)} value={ev} k={ek} />
        ))}
      </div>
    </details>
  )
}

export default JsonTree
