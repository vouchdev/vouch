/** Client for the dev-server claude bridge (plugins/claude-bridge.ts). */

export interface ClaudeEvent {
  type: string
  subtype?: string
  session_id?: string
  result?: string
  is_error?: boolean
  message?: { content?: Array<{ type: string; text?: string; name?: string }> }
  [k: string]: unknown
}

export interface ClaudeRunOptions {
  prompt: string
  resume?: string | null
  bypassPermissions?: boolean
  signal?: AbortSignal
}

/** The Start Here handoff from the Claims tab to the chat. */
export const START_HERE_KEY = 'vouch-ui.start-here'

export interface StartHerePayload {
  claimId: string
  text: string
  sessionId: string | null
}

export function stashStartHere(p: StartHerePayload): void {
  sessionStorage.setItem(START_HERE_KEY, JSON.stringify(p))
}

export function takeStartHere(): StartHerePayload | null {
  try {
    const raw = sessionStorage.getItem(START_HERE_KEY)
    if (!raw) return null
    sessionStorage.removeItem(START_HERE_KEY)
    return JSON.parse(raw) as StartHerePayload
  } catch {
    return null
  }
}

/**
 * Run one claude turn and invoke onEvent per stream-json line. Resolves when
 * the stream ends. Throws on transport-level failure (bridge unreachable).
 */
export async function runClaude(
  opts: ClaudeRunOptions,
  onEvent: (e: ClaudeEvent) => void,
): Promise<void> {
  const res = await fetch('/claude/run', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-claude-bridge': '1' },
    body: JSON.stringify({
      prompt: opts.prompt,
      resume: opts.resume ?? undefined,
      bypassPermissions: opts.bypassPermissions,
    }),
    signal: opts.signal,
  })
  if (!res.ok) {
    let message = `claude bridge failed (${res.status})`
    try {
      const body = (await res.json()) as { error?: { message?: string } }
      if (body.error?.message) message = body.error.message
    } catch {
      /* keep default */
    }
    throw new Error(message)
  }
  if (!res.body) throw new Error('claude bridge returned no body')
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let nl: number
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim()
      buf = buf.slice(nl + 1)
      if (line === '') continue
      try {
        onEvent(JSON.parse(line) as ClaudeEvent)
      } catch {
        /* skip malformed line */
      }
    }
  }
  const tail = buf.trim()
  if (tail !== '') {
    try {
      onEvent(JSON.parse(tail) as ClaudeEvent)
    } catch {
      /* skip malformed tail */
    }
  }
}
