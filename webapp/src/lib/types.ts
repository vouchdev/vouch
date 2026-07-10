export interface VouchConnectionInfo {
  endpoint: string
  token?: string
  /** Display name for this project; defaults to the endpoint's host:port. */
  label?: string
}

export interface Envelope<T> {
  id: string | null
  ok: boolean
  result?: T
  error?: { code: string; message: string }
}

export type Confidence = 'low' | 'medium' | 'high'

export interface SynthesizeResult {
  query: string
  answer: string
  claims: string[]
  gaps: string[]
  _meta?: { synthesis_confidence?: Confidence }
}

export interface SearchHit {
  kind: string
  id: string
  snippet: string
  score: number
  backend: string
}

export interface SearchResult {
  backend: string
  viewer?: { project: string | null; agent: string | null }
  hits: SearchHit[]
}

export interface Claim {
  id: string
  text: string
  type: string
  status: string
  confidence: number
  created_at?: string
  [k: string]: unknown
}

export interface Page {
  id: string
  title: string
  body: string
  type: string
  status: string
  created_at?: string
  [k: string]: unknown
}

export interface Entity {
  id: string
  name: string
  type: string
  created_at?: string
  [k: string]: unknown
}

export interface Relation {
  id: string
  source: string
  relation: string
  target: string
  confidence: number
  created_at?: string
  [k: string]: unknown
}

export interface Proposal {
  id: string
  kind: string
  proposed_by: string
  session_id: string | null
  payload: Record<string, unknown>
  status: string
  proposed_at?: string
  rationale?: string | null
  decided_at?: string | null
  decided_by?: string | null
  decision_reason?: string | null
  [k: string]: unknown
}

/** One row of kb.list_sessions — a captured agent session in the summary pipeline. */
export interface SessionEntry {
  /** Null when the capture never recorded a session id (legacy buffers). */
  session_id: string | null
  /** "buffer" = still-open capture buffer; "pending" = filed proposal awaiting review. */
  stage: 'buffer' | 'pending'
  proposal_id: string | null
  kind: 'claim' | 'page' | null
  title: string | null
  /** False until the configured LLM has produced a narrative summary. */
  summarized: boolean
  observations: number | null
  last_activity: string | null
}

export interface KbStatus {
  kb_dir: string
  claims: number
  pages: number
  sources: number
  entities: number
  relations: number
  evidence: number
  sessions: number
  pending_proposals: number
  audit_events: number
  index_present: boolean
}

export interface KbStats {
  kb_dir?: string
  generated_at: string
  counts: KbStatus
  pending: {
    total: number
    by_agent: Record<string, number>
    age_days: { median: number | null; max: number | null; oldest_id: string | null }
  }
  review: {
    window_days: number | null
    decided_in_window: number
    approved: number
    rejected: number
    expired: number
    approval_rate: number | null
    audit_totals?: { approved: number; rejected: number; expired: number }
    by_agent: Record<string, { approved: number; rejected: number; expired: number; pending: number }>
  }
  citations: {
    claims_total: number
    claims_loadable?: number
    claims_with_valid_citation: number
    broken_citation: number
    invalid_claim: number
    coverage_rate: number | null
  }
}

/** kb.activity — audit-log buckets for the Dashboard view. */
export interface KbActivity {
  generated_at: string
  window_days: number | null
  tz_offset_minutes: number
  viewer?: { project: string | null; agent: string | null }
  total_events: number
  active_days: number
  first_event_day: string | null
  last_event_day: string | null
  /** Keyed by local date "YYYY-MM-DD" (per tz_offset_minutes). */
  by_day: Record<string, { total: number; proposals: number; decisions: number }>
  /** [weekday][hour] counts, weekday 0 = Monday, hour 0-23 local. */
  by_hour: number[][]
  by_actor: Record<string, number>
  by_event: Record<string, number>
}

export interface Capabilities {
  name: string
  level: number
  methods: string[]
  review_gated: boolean
  [k: string]: unknown
}

export interface WhyEdge {
  kind: string
  target: string
  target_kind: string
  event_ts: string | null
  session_id: string | null
  cycle: boolean
  children: WhyEdge[]
}

export interface WhyResult {
  schema_version?: number
  root: string
  node_kind: string
  depth: number
  provenance: WhyEdge[]
}

/** kb.cite returns heterogeneous evidence/source dicts — render defensively. */
export type Citation = Record<string, unknown>
