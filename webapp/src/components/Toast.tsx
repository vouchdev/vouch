import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

type Kind = 'info' | 'success' | 'error'
interface ToastItem {
  id: number
  kind: Kind
  text: string
}

const ToastCtx = createContext<{ toast: (kind: Kind, text: string) => void } | null>(null)

export function useToast() {
  const v = useContext(ToastCtx)
  if (!v) throw new Error('useToast outside ToastProvider')
  return v
}

/**
 * Fire an error toast (code: message) once per error TRANSITION (false -> true).
 * Polling queries keep producing new `error` object identities on every failed
 * refetch while `isError` stays true; without tracking the previous `isError`
 * value this effect would re-toast on every poll tick. Once `isError` goes
 * back to false the ref resets, so a later, distinct failure toasts again.
 */
export function useErrorToast(isError: boolean, error: unknown) {
  const { toast } = useToast()
  const wasError = useRef(false)
  useEffect(() => {
    if (isError && !wasError.current) {
      const code = (error as { code?: string } | null)?.code
      const msg = error instanceof Error ? error.message : String(error)
      toast('error', code ? `${code}: ${msg}` : msg)
    }
    wasError.current = isError
  }, [isError, error, toast])
}

const KIND_CLASS: Record<Kind, string> = {
  info: 'border-rule text-ink-2',
  success: 'border-ok/50 text-ok',
  error: 'border-accent/50 text-accent-2',
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const nextId = useRef(1)
  const timeouts = useRef(new Set<ReturnType<typeof setTimeout>>())

  const toast = useCallback((kind: Kind, text: string) => {
    const id = nextId.current++
    setItems((prev) => [...prev, { id, kind, text }])
    const handle = setTimeout(() => {
      timeouts.current.delete(handle)
      setItems((prev) => prev.filter((t) => t.id !== id))
    }, 5000)
    timeouts.current.add(handle)
  }, [])

  // Clear any pending dismiss timers on unmount so they don't fire (and try
  // to setState) after the provider is gone.
  useEffect(() => {
    const pending = timeouts.current
    return () => {
      pending.forEach(clearTimeout)
      pending.clear()
    }
  }, [])

  return (
    <ToastCtx.Provider value={{ toast }}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-80 flex-col gap-2">
        {items.map((t) => (
          <div
            key={t.id}
            role="status"
            className={`pointer-events-auto rounded-xl border bg-paper-2 px-4 py-3 text-sm shadow-xl ${KIND_CLASS[t.kind]}`}
          >
            {t.text}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}
