import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Trash2 } from 'lucide-react'
import { useState } from 'react'
import type { ProjectState } from '../connection/ConnectionContext'
import { rpc, VouchRpcError } from '../lib/rpc'
import { useToast } from './Toast'

/**
 * Delete action for a durable artifact. Files a review-gated `kb.propose_delete`
 * (the artifact is removed only after approval in Pending), so this never
 * mutates the KB directly. Self-hides when the endpoint does not advertise
 * `kb.propose_delete`. A referenced-block refusal surfaces verbatim as a toast.
 */
export function DeleteArtifactButton({
  project,
  kind,
  id,
  onDone,
}: {
  project: ProjectState
  kind: 'claim' | 'page' | 'entity' | 'relation'
  id: string
  onDone?: () => void
}) {
  const { toast } = useToast()
  const qc = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const has = project.caps?.methods.includes('kb.propose_delete') ?? false

  const del = useMutation({
    mutationFn: () =>
      rpc<{ proposal_id: string; status: string; kind: string }>(project.conn, 'kb.propose_delete', {
        target_kind: kind,
        target_id: id,
      }),
    onSuccess: () => {
      toast('success', 'Delete proposal filed — approve in Pending')
      void qc.invalidateQueries({ queryKey: ['pending'] })
      void qc.invalidateQueries({ queryKey: ['stats'] })
      setConfirming(false)
      onDone?.()
    },
    onError: (err) => {
      const code = err instanceof VouchRpcError ? err.code : undefined
      const message = err instanceof Error ? err.message : String(err)
      toast('error', code ? `${code}: ${message}` : message)
      setConfirming(false)
    },
  })

  if (!has) return null

  if (confirming) {
    return (
      <div className="rounded-xl border border-accent/40 bg-paper-2 p-4">
        <p className="mb-3 text-sm text-ink-2">
          Delete this {kind}? This files a review-gated delete proposal — it is removed only after
          approval in Pending, and is refused if another artifact still references it.
        </p>
        <div className="flex gap-2">
          <button
            onClick={() => del.mutate()}
            disabled={del.isPending}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-paper transition hover:bg-accent-2 disabled:opacity-40"
          >
            Confirm delete
          </button>
          <button
            onClick={() => setConfirming(false)}
            className="rounded-lg border border-rule px-4 py-2 text-sm text-ink-2 transition hover:bg-paper-3"
          >
            Cancel
          </button>
        </div>
      </div>
    )
  }

  return (
    <button
      onClick={() => setConfirming(true)}
      className="flex items-center gap-2 rounded-lg border border-accent/40 px-4 py-2 text-sm font-semibold text-accent-2 transition hover:bg-accent/10"
    >
      <Trash2 size={15} /> Delete
    </button>
  )
}
