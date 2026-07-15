import { SendHorizontal, Search as SearchIcon, Terminal, Trash2, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ArtifactDrawer } from '../components/ArtifactDrawer'
import type { DrawerTarget } from '../components/ArtifactDrawer'
import { EmptyState } from '../components/EmptyState'
import { Markdown } from '../components/Markdown'
import { useToast } from '../components/Toast'
import { useConnection } from '../connection/ConnectionContext'
import { parseAnswer, parseSnippet } from '../lib/citations'
import { runClaude, takeStartHere } from '../lib/claude'
import type { ClaudeEvent } from '../lib/claude'
import { rpc, VouchRpcError } from '../lib/rpc'
import type { Confidence, SearchHit, SearchResult, SynthesizeResult } from '../lib/types'

export type ChatMessage =
  | { role: 'user'; text: string }
  | { role: 'vouch'; kind: 'answer'; result: SynthesizeResult }
  | { role: 'vouch'; kind: 'search'; query: string; result: SearchResult }
  | { role: 'vouch'; kind: 'error'; code?: string; message: string }
  | { role: 'claude'; text: string; isError?: boolean; sessionId?: string }

export function chatStorageKey(endpoint: string): string {
  return `vouch-ui.chat.${endpoint}`
}

const MAX_MESSAGES = 100

function loadThread(endpoint: string): ChatMessage[] {
  try {
    const raw = endpoint ? localStorage.getItem(chatStorageKey(endpoint)) : null
    return raw ? (JSON.parse(raw) as ChatMessage[]) : []
  } catch {
    return []
  }
}

const CONFIDENCE_CLASS: Record<Confidence, string> = {
  high: 'text-ok border-ok/40',
  medium: 'text-accent-2 border-accent/40',
  low: 'text-sepia border-rule',
}

function ConfidenceBadge({ level }: { level?: Confidence }) {
  if (!level) return null
  return (
    <span className={`rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide ${CONFIDENCE_CLASS[level]}`}>
      {level}
    </span>
  )
}

function AnswerBody({
  result,
  onCite,
}: {
  result: SynthesizeResult
  onCite: (kind: 'claim' | 'page', id: string) => void
}) {
  const citeKind = (id: string): 'claim' | 'page' =>
    result.pages?.includes(id) ? 'page' : 'claim'
  if (result.answer === '') {
    return (
      <div>
        <p className="text-sm italic text-sepia">
          No approved claims matched this query — the KB stays silent rather than guessing.
        </p>
        {result.gaps.length > 0 && (
          <p className="mt-2 text-xs text-sepia">
            not covered:{' '}
            {result.gaps.map((g) => (
              <span key={g} className="mr-1 rounded bg-paper-3 px-1.5 py-0.5 font-mono">{g}</span>
            ))}
          </p>
        )}
      </div>
    )
  }
  return (
    <div>
      <p className="text-sm leading-7 text-ink">
        {parseAnswer(result.answer).map((seg, i) =>
          seg.kind === 'text' ? (
            <span key={i}>{seg.text}</span>
          ) : (
            <button
              key={i}
              onClick={() => onCite(citeKind(seg.claimId), seg.claimId)}
              className="mx-0.5 inline-block max-w-72 truncate rounded border border-accent/40 bg-accent/10 px-1.5 align-middle font-mono text-[11px] leading-5 text-accent-2 transition hover:bg-accent/20"
              title={seg.claimId}
            >
              {seg.claimId}
            </button>
          ),
        )}
      </p>
      <div className="mt-2 flex items-center gap-2">
        <ConfidenceBadge level={result._meta?.synthesis_confidence} />
        {result._meta?.synthesis_backend === 'llm' && (
          <span
            data-testid="synthesis-backend"
            className="rounded-full border border-rule px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide text-sepia"
          >
            llm
          </span>
        )}
        {result.gaps.length > 0 && (
          <span className="text-xs text-sepia">
            gaps: {result.gaps.map((g) => (
              <span key={g} className="mr-1 rounded bg-paper-3 px-1.5 py-0.5 font-mono">{g}</span>
            ))}
          </span>
        )}
      </div>
    </div>
  )
}

const DRAWER_KINDS = new Set(['claim', 'page', 'entity', 'relation'])

function HitSnippet({ snippet }: { snippet: string }) {
  return (
    <span className="text-sm text-ink-2">
      {parseSnippet(snippet).map((seg, i) =>
        seg.kind === 'match' ? (
          <mark key={i} className="rounded bg-accent/20 px-0.5 text-accent-2">{seg.text}</mark>
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )}
    </span>
  )
}

function SearchHits({
  result,
  onOpen,
}: {
  result: SearchResult
  onOpen: (kind: 'claim' | 'page' | 'entity' | 'relation', id: string) => void
}) {
  if (result.hits.length === 0) {
    return <p className="text-sm italic text-sepia">No hits.</p>
  }
  return (
    <div className="space-y-2">
      <p className="text-xs text-sepia">
        {result.hits.length} hit{result.hits.length === 1 ? '' : 's'} ·{' '}
        <span data-testid="search-backend" className="font-mono">{result.backend}</span>
      </p>
      {result.hits.map((hit: SearchHit) => {
        const clickable = DRAWER_KINDS.has(hit.kind)
        const body = (
          <>
            <div className="mb-1 flex items-center gap-2">
              <span className="rounded bg-paper-3 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-widest text-accent">
                {hit.kind}
              </span>
              <span className="truncate font-mono text-xs text-sepia">{hit.id}</span>
            </div>
            <HitSnippet snippet={hit.snippet} />
          </>
        )
        return clickable ? (
          <button
            key={hit.id}
            onClick={() => onOpen(hit.kind as 'claim' | 'page' | 'entity' | 'relation', hit.id)}
            className="block w-full rounded-xl border border-rule bg-paper px-3 py-2.5 text-left transition hover:border-accent/50"
          >
            {body}
          </button>
        ) : (
          <div key={hit.id} className="rounded-xl border border-rule/60 bg-paper px-3 py-2.5">{body}</div>
        )
      })}
    </div>
  )
}

export function ChatView() {
  const { active, conn, caps, hasMethod, reportError } = useConnection()
  const { toast } = useToast()
  const [searchParams] = useSearchParams()
  // Gate only once capabilities have loaded; a null caps means "still checking".
  const canAsk = !caps || hasMethod('kb.synthesize')
  const canSearch = !caps || hasMethod('kb.search')
  const endpoint = conn?.endpoint ?? ''
  const [messages, setMessages] = useState<ChatMessage[]>(() => loadThread(endpoint))
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [searchMode, setSearchMode] = useState(false)
  const [claudeMode, setClaudeMode] = useState(() => searchParams.get('mode') === 'claude')
  const [claudeSession, setClaudeSession] = useState<string | null>(null)
  const [activity, setActivity] = useState<string | null>(null)
  const [drawer, setDrawer] = useState<DrawerTarget>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  // Start Here handoff from the Claims tab: enter claude mode bound to the
  // claim's originating session, with the claim quoted in the composer.
  useEffect(() => {
    const start = takeStartHere()
    if (!start) return
    setClaudeMode(true)
    setClaudeSession(start.sessionId)
    setInput(`Start from this approved claim:\n"${start.text}"\n\n`)
  }, [])
  // Tracks which endpoint `messages` currently belongs to. ChatView stays
  // mounted across disconnect->reconnect (ConnectDialog is an overlay, not a
  // route swap), so `endpoint` can change out from under this component. If
  // we let the persist effect below run unconditionally on every change, it
  // would write the OLD endpoint's messages under the NEW endpoint's storage
  // key before the reload catches up — silently clobbering that thread. So
  // this ref is checked first: on an endpoint change we swap in the new
  // endpoint's stored thread and bail before persisting anything.
  const threadEndpointRef = useRef(endpoint)

  useEffect(() => {
    if (threadEndpointRef.current !== endpoint) {
      threadEndpointRef.current = endpoint
      setMessages(loadThread(endpoint))
      return
    }
    if (!endpoint) return
    if (messages.length === 0) {
      localStorage.removeItem(chatStorageKey(endpoint))
    } else {
      localStorage.setItem(chatStorageKey(endpoint), JSON.stringify(messages.slice(-MAX_MESSAGES)))
    }
  }, [messages, endpoint])

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: 'smooth' })
  }, [messages.length])

  async function runClaudeTurn(text: string) {
    setBusy(true)
    setMessages((m) => [...m, { role: 'user', text }])
    setActivity('claude is starting…')
    let nextSession: string | null = claudeSession
    let sawResult = false
    let tools = 0
    try {
      await runClaude(
        { prompt: text, resume: claudeSession, bypassPermissions: true },
        (e: ClaudeEvent) => {
          if (e.type === 'system' && e.subtype === 'init' && typeof e.session_id === 'string') {
            nextSession = e.session_id
            setActivity('claude is working…')
          } else if (e.type === 'assistant') {
            const blocks = e.message?.content ?? []
            for (const b of blocks) {
              if (b.type === 'tool_use') {
                tools += 1
                setActivity(`claude is working… (${tools} tool use${tools === 1 ? '' : 's'}${b.name ? `, last: ${b.name}` : ''})`)
              }
            }
          } else if (e.type === 'result') {
            sawResult = true
            if (typeof e.session_id === 'string') nextSession = e.session_id
            setMessages((m) => [
              ...m,
              {
                role: 'claude',
                text: typeof e.result === 'string' && e.result !== '' ? e.result : '(no output)',
                isError: e.is_error === true,
                sessionId: typeof e.session_id === 'string' ? e.session_id : undefined,
              },
            ])
          } else if (e.type === 'bridge_error') {
            sawResult = true
            setMessages((m) => [
              ...m,
              { role: 'claude', text: String(e.message ?? 'claude bridge error'), isError: true },
            ])
          }
        },
      )
      if (!sawResult) {
        setMessages((m) => [...m, { role: 'claude', text: 'claude run ended without a result', isError: true }])
      }
      setClaudeSession(nextSession)
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      toast('error', message)
      setMessages((m) => [...m, { role: 'claude', text: message, isError: true }])
    } finally {
      setActivity(null)
      setBusy(false)
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault()
    const text = input.trim()
    if (text === '' || busy) return
    if (claudeMode) {
      setInput('')
      void runClaudeTurn(text)
      return
    }
    if (!conn) return
    // The endpoint this request belongs to. If the user switches endpoints
    // while the rpc is in flight, the thread reloads for the new endpoint —
    // a late response must then be dropped, not appended (and persisted)
    // into the new endpoint's thread.
    const submittedFor = endpoint
    setInput('')
    setBusy(true)
    setMessages((m) => [...m, { role: 'user', text }])
    try {
      if (searchMode || text.startsWith('/search ')) {
        const query = (text.startsWith('/search ') ? text.slice('/search '.length) : text).trim()
        const result = await rpc<SearchResult>(conn, 'kb.search', { query, limit: 10 })
        if (threadEndpointRef.current !== submittedFor) return
        setMessages((m) => [...m, { role: 'vouch', kind: 'search', query, result }])
      } else {
        // LLM-first: the endpoint drafts the answer with its configured
        // compile.llm_cmd, grounded in KB pages + approved claims. Endpoints
        // without an llm_cmd reject with "not configured" — fall back to
        // deterministic claim synthesis so the chat still answers.
        let result: SynthesizeResult
        try {
          result = await rpc<SynthesizeResult>(conn, 'kb.synthesize', { query: text, llm: true })
        } catch (err) {
          if (!(err instanceof VouchRpcError) || !/not configured/i.test(err.message)) throw err
          result = await rpc<SynthesizeResult>(conn, 'kb.synthesize', { query: text })
        }
        if (threadEndpointRef.current !== submittedFor) return
        setMessages((m) => [...m, { role: 'vouch', kind: 'answer', result }])
      }
    } catch (err) {
      reportError(err) // 401 gate is connection-level (spec §4) — never skipped, even for a stale response
      if (threadEndpointRef.current !== submittedFor) return
      const code = err instanceof VouchRpcError ? err.code : undefined
      const message = err instanceof Error ? err.message : String(err)
      toast('error', code ? `${code}: ${message}` : message)
      setMessages((m) => [...m, { role: 'vouch', kind: 'error', code, message }])
    } finally {
      setBusy(false)
    }
  }

  function clear() {
    setMessages([])
    if (endpoint) localStorage.removeItem(chatStorageKey(endpoint))
  }

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col px-6">
      <div className="min-h-0 flex-1 overflow-y-auto py-6">
        {messages.length === 0 ? (
          claudeMode ? (
            <EmptyState
              title="Order Claude Code"
              hint="Messages here run Claude Code headless in the project workspace — ask it to fix, build, or investigate. Proposals it makes still land in the review queue."
            />
          ) : (
            <EmptyState
              title="Ask your knowledge base"
              hint="Answers are grounded in your KB's pages and approved claims, every citation verified — nothing is guessed. Try a question about knowledge your agents have vouched, or run /search <term>."
            />
          )
        ) : (
          <div className="space-y-4">
            {messages.map((msg, i) => {
              if (msg.role === 'user') {
                return (
                  <div key={i} className="flex justify-end">
                    <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-paper-3 px-4 py-2.5 text-sm text-ink">
                      {msg.text}
                    </div>
                  </div>
                )
              }
              if (msg.role === 'claude') {
                return (
                  <div key={i} className="flex">
                    <div
                      className={`w-full max-w-[85%] rounded-2xl rounded-bl-sm border px-4 py-3 ${
                        msg.isError ? 'border-accent/40 bg-accent/10' : 'border-rule bg-paper-2'
                      }`}
                    >
                      <div className="mb-1.5 flex items-center gap-2">
                        <span className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-accent">
                          <Terminal size={11} /> claude code
                        </span>
                        {msg.sessionId && (
                          <span className="font-mono text-[10px] text-sepia">{msg.sessionId.slice(0, 8)}</span>
                        )}
                      </div>
                      {msg.isError ? (
                        <p className="whitespace-pre-wrap text-sm text-accent-2">{msg.text}</p>
                      ) : (
                        <Markdown>{msg.text}</Markdown>
                      )}
                    </div>
                  </div>
                )
              }
              if (msg.kind === 'error') {
                return (
                  <div key={i} className="flex">
                    <div className="max-w-[85%] rounded-2xl rounded-bl-sm border border-accent/40 bg-accent/10 px-4 py-2.5 text-sm">
                      {msg.code && <span className="mr-2 font-mono text-xs text-accent">{msg.code}</span>}
                      <span className="text-accent-2">{msg.message}</span>
                    </div>
                  </div>
                )
              }
              if (msg.kind === 'answer') {
                return (
                  <div key={i} className="flex">
                    <div className="max-w-[85%] rounded-2xl rounded-bl-sm border border-rule bg-paper-2 px-4 py-3">
                      <AnswerBody result={msg.result} onCite={(kind, id) => setDrawer({ kind, id })} />
                    </div>
                  </div>
                )
              }
              if (msg.kind === 'search') {
                return (
                  <div key={i} className="flex">
                    <div className="w-full max-w-[85%] rounded-2xl rounded-bl-sm border border-rule bg-paper-2 px-4 py-3">
                      <SearchHits result={msg.result} onOpen={(kind, id) => setDrawer({ kind, id })} />
                    </div>
                  </div>
                )
              }
              return null
            })}
            {activity && (
              <div className="flex">
                <div className="rounded-2xl rounded-bl-sm border border-rule/60 bg-paper-2 px-4 py-2.5 text-sm italic text-sepia">
                  {activity}
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      {claudeMode && claudeSession && (
        <div className="flex items-center gap-2 border-t border-rule pt-3 text-xs text-sepia">
          <Terminal size={12} className="text-accent" />
          <span>
            resuming session <span className="font-mono text-ink-2">{claudeSession.slice(0, 8)}</span>
          </span>
          <button
            type="button"
            aria-label="detach session"
            title="Detach — next message starts a fresh Claude Code session"
            onClick={() => setClaudeSession(null)}
            className="rounded p-0.5 text-sepia transition hover:bg-paper-3 hover:text-ink"
          >
            <X size={12} />
          </button>
        </div>
      )}
      <form
        onSubmit={submit}
        className={`flex items-center gap-2 py-4 ${claudeMode && claudeSession ? '' : 'border-t border-rule'}`}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={
            claudeMode
              ? 'Order Claude Code — it runs in the project workspace'
              : searchMode
                ? canSearch
                  ? 'Search the KB index…'
                  : 'kb.search is not advertised by this endpoint'
                : canAsk
                  ? 'Ask the KB — or /search <query>'
                  : 'kb.synthesize is not advertised by this endpoint'
          }
          disabled={busy || (!claudeMode && !((canAsk && !searchMode) || (canSearch && searchMode)))}
          className="min-w-0 flex-1 rounded-xl border border-rule bg-paper-2 px-4 py-2.5 text-sm text-ink outline-none placeholder:text-sepia focus:border-accent disabled:opacity-60"
        />
        <button
          type="button"
          aria-label="claude mode"
          aria-pressed={claudeMode}
          title={claudeMode ? 'Back to KB chat' : 'Claude Code mode — order commands, they run in the project workspace'}
          onClick={() => setClaudeMode((c) => !c)}
          className={`rounded-xl border p-2.5 transition ${
            claudeMode ? 'border-accent/60 bg-accent/10 text-accent-2' : 'border-rule text-sepia hover:bg-paper-3'
          }`}
        >
          <Terminal size={16} />
        </button>
        <button
          type="button"
          aria-label="search mode"
          aria-pressed={searchMode}
          disabled={!canSearch}
          title={
            canSearch
              ? 'Toggle search mode (kb.search instead of kb.synthesize)'
              : 'kb.search is not advertised by this endpoint'
          }
          onClick={() => setSearchMode((s) => !s)}
          className={`rounded-xl border p-2.5 transition disabled:cursor-not-allowed disabled:opacity-40 ${
            searchMode ? 'border-accent/60 bg-accent/10 text-accent-2' : 'border-rule text-sepia hover:bg-paper-3'
          }`}
        >
          <SearchIcon size={16} />
        </button>
        <button
          type="submit"
          disabled={
            busy ||
            (!claudeMode && !((canAsk && !searchMode) || (canSearch && searchMode))) ||
            input.trim() === ''
          }
          aria-label="send"
          className="rounded-xl bg-accent p-2.5 text-paper transition hover:bg-accent-2 disabled:opacity-40"
        >
          <SendHorizontal size={16} />
        </button>
        <button
          type="button"
          onClick={clear}
          aria-label="clear thread"
          title="Clear thread"
          className="rounded-xl border border-rule p-2.5 text-sepia transition hover:bg-paper-3 hover:text-ink"
        >
          <Trash2 size={16} />
        </button>
      </form>

      <ArtifactDrawer target={drawer} project={active} onClose={() => setDrawer(null)} />
    </div>
  )
}
