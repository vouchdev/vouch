// Dashboard.tsx — bespoke dashboard view. Faithful port of renderDashboard /
// dashCard / kv / flatKvs from app.js:189-231.
//
// Class names: dash-grid dash-card dash-loading kv k v actions btn ghost sm

import { useEffect, useState } from 'react'
import * as api from '../lib/client'
import { useVouch } from '../lib/VouchContext'
import { isAvailable } from '../lib/VouchContext'

// ---------------------------------------------------------------------------
// Helpers — faithful ports of dashCard / kv / flatKvs
// ---------------------------------------------------------------------------

interface KvRow {
  k: string
  v: string
}

function buildKv(k: string, v: unknown): KvRow {
  return { k, v: String(v ?? '—') }
}

function flatKvs(o: unknown, prefix = ''): KvRow[] {
  const rows: KvRow[] = []
  for (const [k, v] of Object.entries(o as Record<string, unknown> ?? {})) {
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      rows.push(...flatKvs(v, prefix + k + '.'))
    } else {
      rows.push(buildKv(prefix + k, Array.isArray(v) ? v.length : v))
    }
  }
  return rows.slice(0, 14)
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Kv({ k, v }: KvRow) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  )
}

function DashCard({ title, rows }: { title: string; rows: KvRow[] }) {
  return (
    <div className="dash-card">
      <h3>{title}</h3>
      {rows.map((r, i) => (
        <Kv key={i} k={r.k} v={r.v} />
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Dashboard view
// ---------------------------------------------------------------------------

export default function Dashboard() {
  const { state, actions } = useVouch()
  const { navigate } = actions
  const caps = state.caps ?? {}

  const [statusRows, setStatusRows] = useState<KvRow[] | null>(null)
  const [statusErr, setStatusErr] = useState<string | null>(null)
  const [statsRows, setStatsRows] = useState<KvRow[] | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false

    // Reset to fresh loading state whenever root or capMethods change,
    // matching the original app.js behaviour that always starts from a cleared
    // DOM node (no stale data visible while the new KB loads).
    setLoading(true)
    setStatusRows(null)
    setStatusErr(null)
    setStatsRows(null)

    async function load() {
      // status
      try {
        const s = await api.call<Record<string, unknown>>('kb.status', {})
        if (cancelled) return
        const counts = (s.counts ?? s.artifacts ?? s) as Record<string, unknown>
        const rows = Object.entries(counts)
          .filter(([, v]) => typeof v === 'number')
          .map(([k, v]) => buildKv(k, v))
        setStatusRows(rows)
      } catch (e) {
        if (!cancelled) setStatusErr((e as Error).message)
      }

      // stats (if available) — read capMethods from the effect's own closure
      // so we never use a stale snapshot from a previous render.
      if (isAvailable(state, 'kb.stats')) {
        try {
          const st = await api.call<unknown>('kb.stats', { days: 30 })
          if (!cancelled) setStatsRows(flatKvs(st))
        } catch {
          // older vouch: stats may be absent — silently skip
        }
      }

      if (!cancelled) setLoading(false)
    }

    void load()
    return () => { cancelled = true }
  }, [state.root, state.capMethods])

  return (
    <>
      {loading && <div className="dash-loading muted">loading…</div>}
      <div className="dash-grid">
        {/* capabilities card */}
        <DashCard
          title="capabilities"
          rows={[
            buildKv('version', caps.version),
            buildKv('spec', caps.spec),
            buildKv('methods', (caps.methods ?? []).length),
            buildKv('retrieval', (caps.retrieval ?? []).join(', ')),
            buildKv('review gated', String(!!(caps as { review_gated?: boolean }).review_gated)),
          ]}
        />

        {/* status card */}
        {statusErr != null ? (
          <div className="dash-card">
            <h3>status</h3>
            <div className="muted">{statusErr}</div>
          </div>
        ) : statusRows != null ? (
          <DashCard title="status" rows={statusRows} />
        ) : null}

        {/* stats (30d) card */}
        {statsRows != null && <DashCard title="stats (30d)" rows={statsRows} />}

        {/* quick actions */}
        <div className="dash-card actions">
          <h3>quick actions</h3>
          <button className="btn ghost sm" onClick={() => navigate('search')}>
            Search &amp; Ask
          </button>
          <button className="btn ghost sm" onClick={() => navigate('review')}>
            Review queue
          </button>
          <button className="btn ghost sm" onClick={() => navigate('propose')}>
            Propose
          </button>
          <button className="btn ghost sm" onClick={() => navigate('dual-solve')}>
            Dual-Solve
          </button>
        </div>
      </div>
    </>
  )
}
