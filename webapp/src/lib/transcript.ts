import { rpc } from './rpc'
import type { VouchConnectionInfo } from './types'

export interface Tokens {
  input: number
  output: number
  cache_read: number
  cache_creation: number
}

export interface ToolResult {
  content: string
  is_error: boolean
  subagent_session_id: string | null
}

export type TranscriptBlock =
  | { type: 'text'; text: string }
  | { type: 'thinking'; text: string }
  | {
      type: 'tool_use'
      id: string | null
      name: string
      input: Record<string, unknown>
      result: ToolResult | null
    }

export interface TranscriptMessage {
  role: 'user' | 'assistant'
  id: string | null
  model: string | null
  timestamp: string | null
  tokens: Tokens | null
  blocks: TranscriptBlock[]
}

export interface SessionMeta {
  id: string
  agent: string
  cwd: string | null
  git_branch: string | null
  title: string | null
  started_at: string | null
  ended_at: string | null
  model: string | null
  tokens: Tokens
}

export interface Observation {
  ts: number
  tool: string
  summary: string
  files?: string[]
  cmd?: string
}

export type Transcript =
  | {
      available: true
      source: { agent: string; path: string }
      session: SessionMeta
      messages: TranscriptMessage[]
      truncated: boolean
    }
  | { available: false; reason: string; observations: Observation[] }

export function fetchTranscript(
  conn: VouchConnectionInfo,
  sessionId: string,
  agent?: string,
): Promise<Transcript> {
  const params: Record<string, unknown> = { session_id: sessionId }
  if (agent) params.agent = agent
  return rpc<Transcript>(conn, 'kb.session_transcript', params)
}
