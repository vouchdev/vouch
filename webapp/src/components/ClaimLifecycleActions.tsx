import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Archive, ArrowRightLeft } from 'lucide-react'
import { useState } from 'react'
import type { ProjectState } from '../connection/ConnectionContext'
import { rpc, VouchRpcError } from '../lib/rpc'
import { useToast } from './Toast'

/**
 * Claim-only lifecycle actions — Archive (hide from retrieval, keep the file +
 * citations) and Supersede (replace with another claim, preserving history).
 * These are direct lifecycle ops (not review-gated), so they take effect
 * immediately. Each button self-hides when its method isn't advertised; the
 * component renders nothing when neither is.
 */
export function ClaimLifecycleActions({
  project,
  claimId,
  onDone,
}: {
  project: ProjectState
  claimId: string
  onDone?: () => void
}) {
  const { toast } = useToast()
  const qc = useQueryClient()
  const [mode, setMode] = useState<'idle' | 'archive' | 'supersede'>('idle')
  const [newId, setNewId] = useState('')

  const canArchive = project.caps?.methods.includes('kb.archive') ?? false
  const canSupersede = project.caps?.methods.includes('kb.supersede') ?? false

  function invalidate() {
    void qc.invalidateQueries({ queryKey: ['list'] })
    void qc.invalidateQueries({ queryKey: ['stats'] })
    void qc.invalidateQueries({ queryKey: ['artifact'] })
  }

  function onError(err: unknown) {
    const code = err instanceof VouchRpcError ? err.code : undefined
    const message = err instanceof Error ? err.message : String(err)
    toast('error', code ? `${code}: ${message}` : message)
    setMode('idle')
  }

  const archive = useMutation({
    mutationFn: () => rpc(project.conn, 'kb.archive', { claim_id: claimId }),
    onSuccess: () => {
      toast('success', 'Claim archived — hidden from retrieval, citations kept')
      invalidate()
      setMode('idle')
      onDone?.()
    },
    onError,
  })

  const supersede = useMutation({
    mutationFn: (newClaimId: string) =>
      rpc(project.conn, 'kb.supersede', { old_claim_id: claimId, new_claim_id: newClaimId }),
    onSuccess: () => {
      toast('success', 'Claim superseded')
      invalidate()
      setMode('idle')
      setNewId('')
      onDone?.()
    },
    onError,
  })

  if (!canArchive && !canSupersede) return null

  if (mode === 'archive') {
    return (
      <div className="rounded-xl border border-rule bg-paper-2 p-4">
        <p className="mb-3 text-sm text-ink-2">
          Archive this claim? It is hidden from search and retrieval, but its file and every page
          citation stay intact. Reversible.
        </p>
        <div className="flex gap-2">
          <button
            onClick={() => archive.mutate()}
            disabled={archive.isPending}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-paper transition hover:bg-accent-2 disabled:opacity-40"
          >
            Confirm archive
          </button>
          <button
            onClick={() => setMode('idle')}
            className="rounded-lg border border-rule px-4 py-2 text-sm text-ink-2 transition hover:bg-paper-3"
          >
            Cancel
          </button>
        </div>
      </div>
    )
  }

  if (mode === 'supersede') {
    const trimmed = newId.trim()
    const valid = trimmed !== '' && trimmed !== claimId
    return (
      <div className="rounded-xl border border-rule bg-paper-2 p-4">
        <label className="mb-1 block text-xs font-medium text-ink-2" htmlFor="supersede-id">
          Replace this claim with — the id of an existing claim
        </label>
        <input
          id="supersede-id"
          value={newId}
          onChange={(e) => setNewId(e.target.value)}
          placeholder="existing claim id"
          className="mb-3 w-full rounded-lg border border-rule bg-paper px-3 py-2 font-mono text-sm text-ink outline-none focus:border-accent"
        />
        <div className="flex gap-2">
          <button
            onClick={() => supersede.mutate(trimmed)}
            disabled={!valid || supersede.isPending}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-paper transition hover:bg-accent-2 disabled:opacity-40"
          >
            Confirm supersede
          </button>
          <button
            onClick={() => {
              setMode('idle')
              setNewId('')
            }}
            className="rounded-lg border border-rule px-4 py-2 text-sm text-ink-2 transition hover:bg-paper-3"
          >
            Cancel
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex gap-2">
      {canArchive && (
        <button
          onClick={() => setMode('archive')}
          className="flex items-center gap-2 rounded-lg border border-rule px-4 py-2 text-sm font-semibold text-ink-2 transition hover:bg-paper-3"
        >
          <Archive size={15} /> Archive
        </button>
      )}
      {canSupersede && (
        <button
          onClick={() => setMode('supersede')}
          className="flex items-center gap-2 rounded-lg border border-rule px-4 py-2 text-sm font-semibold text-ink-2 transition hover:bg-paper-3"
        >
          <ArrowRightLeft size={15} /> Supersede
        </button>
      )}
    </div>
  )
}
