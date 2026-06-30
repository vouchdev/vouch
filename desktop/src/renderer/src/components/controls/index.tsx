// controls/index.tsx — controlled form-control components ported from
// src/renderer/lib/form-gen.js:60-214 (buildControl + each control function).
//
// Each control is:
//   (props: { param: Param; value: unknown; onChange: (v: unknown)=>void; ctx: FormCtx; id?: string }) => JSX.Element
//
// VALUE / PARSE-ERROR CONTRACT
// ────────────────────────────
// Controls that need parsing (NumberInput, JsonEditor) store their raw string
// in `value` via onChange so that the parent (MethodForm) can re-parse inside
// collect(). If the raw string is unparseable, collect() re-parses, catches the
// error and rethrows as `Error("<paramName>: <msg>")`.
//
// Controls that can always produce a valid output (Text, Textarea, Toggle,
// Slider, Select, Combobox, Tags, Ref, FileControl) emit the coerced value
// directly (or undefined for empty).
//
// TRIM CONTRACT — mirroring form-gen.js behaviour:
//   text / textarea / combobox trim only at collect() time (i.e. on blur /
//   coerceValue). While the user is typing, the raw string (including trailing
//   spaces) is kept in state so cursor-position is not destroyed.

import { useCallback, useEffect, useId, useRef, useState } from 'react'
import type { ControlType, Param } from '../../../../shared/methods.types'
import * as api from '../../lib/client'

// ---------------------------------------------------------------------------
// FormCtx — passed down from MethodForm to every control
// ---------------------------------------------------------------------------
export interface Hit {
  id: string
  kind?: string
  snippet?: string
}

export interface FormCtx {
  search: (q: string, kind?: string) => Promise<Hit[]>
  pickFile: typeof api.pickFile
  pickSave: typeof api.pickSave
}

// ---------------------------------------------------------------------------
// Generic component type
// ---------------------------------------------------------------------------
export type ControlProps = {
  param: Param
  value: unknown
  onChange: (v: unknown) => void
  ctx: FormCtx
  id?: string
}

type ControlComponent = (props: ControlProps) => JSX.Element

// ---------------------------------------------------------------------------
// Text — mirrors textControl (form-gen.js:61-65)
//
// Raw text is kept while typing; trim happens at collect time via coerceValue.
// ---------------------------------------------------------------------------
export function Text({ param, value, onChange, id }: ControlProps): JSX.Element {
  return (
    <input
      id={id}
      type="text"
      className="input"
      placeholder={param.default ?? ''}
      value={typeof value === 'string' ? value : ''}
      onChange={(e) => {
        const v = e.target.value
        onChange(v === '' ? undefined : v)
      }}
    />
  )
}

// ---------------------------------------------------------------------------
// Textarea — mirrors textareaControl (form-gen.js:66-69)
// ---------------------------------------------------------------------------
export function Textarea({ param, value, onChange, id }: ControlProps): JSX.Element {
  return (
    <textarea
      id={id}
      className="input area"
      rows={4}
      placeholder={param.default ?? ''}
      value={typeof value === 'string' ? value : ''}
      onChange={(e) => {
        const v = e.target.value
        onChange(v === '' ? undefined : v)
      }}
    />
  )
}

// ---------------------------------------------------------------------------
// NumberInput — mirrors numberControl (form-gen.js:70-83)
//
// Stores the raw string so collect() can re-parse and surface "not a number".
// ---------------------------------------------------------------------------
export function NumberInput({ param, value, onChange, id }: ControlProps): JSX.Element {
  const isInt = param.control === 'integer'
  return (
    <input
      id={id}
      type="number"
      className="input"
      step={isInt ? '1' : 'any'}
      placeholder={param.default ?? ''}
      // value is stored as-is (raw string or number); display as string
      value={value === undefined || value === null ? '' : String(value)}
      onChange={(e) => {
        const s = e.target.value
        // Store raw string; collect() re-parses
        onChange(s === '' ? undefined : s)
      }}
    />
  )
}

// ---------------------------------------------------------------------------
// Slider — mirrors sliderControl (form-gen.js:84-91)
//
// Emits on every change. MethodForm must initialise slider params with their
// default value so that untouched sliders are not silently omitted from
// collect() (mirrors form-gen.js always reading range.value).
// ---------------------------------------------------------------------------
export function Slider({ param, value, onChange, id }: ControlProps): JSX.Element {
  const rawDefault =
    param.default !== '' && param.default != null ? Number(param.default) : 0.7
  const numDefault = Number.isNaN(rawDefault) ? 0.7 : rawDefault

  const numVal = typeof value === 'number' ? value : numDefault

  // Emit default on first render so parent state is never undefined for a
  // slider that the user never touches (mirrors form-gen.js always-present get).
  useEffect(() => {
    if (value === undefined) {
      onChange(numDefault)
    }
    // Only run on mount — intentionally omitting deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="slider">
      <input
        id={id}
        type="range"
        min="0"
        max="1"
        step="0.05"
        className="range"
        value={numVal}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <span className="range-val">{numVal.toFixed(2)}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Toggle — mirrors toggleControl (form-gen.js:92-96)
//
// Emits on every change. MethodForm must initialise toggle params with their
// default value so that untouched toggles are not silently omitted.
// ---------------------------------------------------------------------------
export function Toggle({ param, value, onChange, id }: ControlProps): JSX.Element {
  const def = String(param.default).toLowerCase() === 'true'
  const checked = value === undefined ? def : Boolean(value)

  // Emit default on first render (same rationale as Slider).
  useEffect(() => {
    if (value === undefined) {
      onChange(def)
    }
    // Only run on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <label className="switch">
      <input
        id={id}
        type="checkbox"
        className="toggle"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="track" />
    </label>
  )
}

// ---------------------------------------------------------------------------
// Select — mirrors selectControl (form-gen.js:97-103)
//
// Pre-selects the default when value is undefined and param.default is in enum
// (mirrors form-gen.js:101: `if (p.default && p.enum.includes(p.default)) s.value = p.default`).
// MethodForm should also initialise state with defaults, but the control
// itself emits the default on mount for safety.
// ---------------------------------------------------------------------------
export function Select({ param, value, onChange, id }: ControlProps): JSX.Element {
  const opts = param.enum ?? []

  // Resolve effective default: use param.default if it is in the enum.
  // For a required select with no usable default, fall back to the first
  // enum option (mirrors the original selectControl where the native select
  // always defaults to its first option for required params).
  const effectiveDefault =
    param.default && opts.includes(param.default)
      ? param.default
      : param.required && opts.length > 0
        ? opts[0]
        : ''

  const strVal =
    value === undefined || value === null
      ? effectiveDefault
      : String(value)

  // Emit pre-selected default on first render (mirrors form-gen.js:101).
  useEffect(() => {
    if (value === undefined && effectiveDefault !== '') {
      onChange(effectiveDefault)
    }
    // Only run on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <select
      id={id}
      className="input"
      value={strVal}
      onChange={(e) => {
        const v = e.target.value
        onChange(v === '' ? undefined : v)
      }}
    >
      {!param.required && <option value="">— any —</option>}
      {opts.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  )
}

// ---------------------------------------------------------------------------
// Combobox — mirrors comboboxControl (form-gen.js:104-109)
//
// Raw text kept while typing; trim at collect time.
// ---------------------------------------------------------------------------
export function Combobox({ param, value, onChange, id }: ControlProps): JSX.Element {
  const listId = useId()
  const opts = param.enum ?? []
  const placeholder = param.default ?? (opts[0] ?? '')

  return (
    <div>
      <input
        id={id}
        className="input"
        list={listId}
        placeholder={placeholder}
        value={typeof value === 'string' ? value : ''}
        onChange={(e) => {
          const v = e.target.value
          onChange(v === '' ? undefined : v)
        }}
      />
      <datalist id={listId}>
        {opts.map((o) => (
          <option key={o} value={o} />
        ))}
      </datalist>
    </div>
  )
}

// ---------------------------------------------------------------------------
// JsonEditor — mirrors jsonControl (form-gen.js:110-120)
//
// Stores raw text so collect() can re-parse and surface "invalid JSON".
// ---------------------------------------------------------------------------
export function JsonEditor({ value, onChange, id }: ControlProps): JSX.Element {
  return (
    <textarea
      id={id}
      className="input area mono"
      rows={4}
      placeholder='{ "key": "value" }'
      value={typeof value === 'string' ? value : ''}
      onChange={(e) => {
        const s = e.target.value
        onChange(s === '' ? undefined : s)
      }}
    />
  )
}

// ---------------------------------------------------------------------------
// Tags — mirrors tagsControl (form-gen.js:123-142) — chip input
//
// Emits string[] (or undefined if empty).
// ---------------------------------------------------------------------------
export function Tags({ value, onChange, id }: ControlProps): JSX.Element {
  const tags: string[] = Array.isArray(value) ? (value as string[]) : []

  const [inputVal, setInputVal] = useState('')

  function addMany(raw: string): void {
    const next = [...tags]
    raw
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean)
      .forEach((s) => {
        if (!next.includes(s)) next.push(s)
      })
    onChange(next.length ? next : undefined)
  }

  function remove(idx: number): void {
    const next = tags.filter((_, i) => i !== idx)
    onChange(next.length ? next : undefined)
  }

  function commitInput(): void {
    if (inputVal.trim()) {
      addMany(inputVal)
      setInputVal('')
    }
  }

  return (
    <div className="tags">
      <div className="chips">
        {tags.map((t, i) => (
          <span key={i} className="chip">
            {t}
            <button
              className="chip-x"
              type="button"
              onClick={() => remove(i)}
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <input
        id={id}
        type="text"
        className="input"
        placeholder="type + Enter, or paste comma/space-separated"
        value={inputVal}
        onChange={(e) => setInputVal(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault()
            commitInput()
          }
        }}
        onBlur={commitInput}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Ref — mirrors refControl (form-gen.js:159-214) — search-backed id typeahead
//
// multi=true → emits string[] (or undefined), single → emits string (or undefined).
// The multi prop comes from param.refMulti, OR is forced true when ctrl==='tags'
// (mirrors form-gen.js:124 which calls refControl(p, id, ctx, true) — hardcoded
// multi=true — whenever ctrl==='tags' AND p.refKind is set).
//
// Single-mode: handleInput flushes typed text to parent via onChange immediately
// (mirrors form-gen.js:211 `i.value.trim() || undefined` read at collect time).
// ---------------------------------------------------------------------------
export function Ref({ param, value, onChange, ctx, id }: ControlProps): JSX.Element {
  // ctrl==='tags' with refKind set → always multi (form-gen.js:124 hardcodes multi=true)
  const multi = param.control === 'tags' ? true : !!param.refMulti
  const picked: string[] = multi && Array.isArray(value) ? (value as string[]) : []
  const singleVal = !multi && typeof value === 'string' ? value : ''

  const [inputVal, setInputVal] = useState(multi ? '' : singleVal)
  const [hits, setHits] = useState<Hit[]>([])
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Cleanup debounce timer on unmount to avoid state-update-on-unmounted warning.
  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current)
    }
  }, [])

  // Sync single non-multi input when value changes externally (e.g., form reset).
  useEffect(() => {
    if (!multi) {
      setInputVal(typeof value === 'string' ? value : '')
    }
  }, [value, multi])

  function choose(idVal: string): void {
    if (multi) {
      const next = picked.includes(idVal) ? picked : [...picked, idVal]
      onChange(next.length ? next : undefined)
      setInputVal('')
    } else {
      onChange(idVal)
      setInputVal(idVal)
    }
    setHits([])
  }

  function removePicked(idx: number): void {
    const next = picked.filter((_, i) => i !== idx)
    onChange(next.length ? next : undefined)
  }

  function handleInput(q: string): void {
    setInputVal(q)
    // Single mode: flush typed value immediately to parent (mirrors form-gen.js:211
    // which reads i.value.trim() directly at collect time).
    if (!multi) {
      onChange(q.trim() || undefined)
    }
    if (timer.current) clearTimeout(timer.current)
    if (q.trim().length < 2 || !ctx.search) {
      setHits([])
      return
    }
    timer.current = setTimeout(async () => {
      try {
        const results = await ctx.search(q.trim(), param.refKind)
        setHits(results.slice(0, 8))
      } catch {
        // search unavailable; plain text entry still works
      }
    }, 220)
  }

  // Commit current text on Enter (multi) — mirrors refControl keydown handler
  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>): void {
    if (multi && e.key === 'Enter' && inputVal.trim()) {
      e.preventDefault()
      choose(inputVal.trim())
    }
  }

  // Commit uncommitted multi-mode text on blur — mirrors form-gen.js:208 which
  // auto-includes the remaining i.value when get() is called.
  function handleBlur(): void {
    if (multi && inputVal.trim()) {
      const next = picked.includes(inputVal.trim())
        ? picked
        : [...picked, inputVal.trim()]
      onChange(next.length ? next : undefined)
      setInputVal('')
      setHits([])
    }
  }

  return (
    <div className="ref">
      {multi && (
        <div className="chips">
          {picked.map((t, i) => (
            <span key={i} className="chip">
              {t}
              <button
                className="chip-x"
                type="button"
                onClick={() => removePicked(i)}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="ref-row">
        <input
          id={id}
          type="text"
          className="input"
          placeholder={`search ${param.refKind ?? 'id'}…  (or paste an id)`}
          value={inputVal}
          onChange={(e) => handleInput(e.target.value)}
          onKeyDown={handleKeyDown}
          onBlur={handleBlur}
        />
        <span className="ref-kind">{param.refKind ?? 'id'}</span>
      </div>
      {hits.length > 0 && (
        <div className="ref-results">
          {hits.map((hit) => (
            <div
              key={hit.id}
              className="ref-hit"
              onClick={() => choose(hit.id)}
            >
              <span className="ref-hit-kind">{hit.kind ?? ''}</span>
              <span className="ref-hit-id">{hit.id}</span>
              <span className="ref-hit-snip">{hit.snippet ?? ''}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// FileControl — mirrors fileControl (form-gen.js:145-156)
// ---------------------------------------------------------------------------
export function FileControl({ param, value, onChange, ctx, id }: ControlProps): JSX.Element {
  const mode = param.file // open | save | under-root
  const strVal = typeof value === 'string' ? value : ''

  const handlePick = useCallback(async () => {
    try {
      const picker = mode === 'save' ? ctx.pickSave : ctx.pickFile
      const path = await picker({ title: `Choose ${param.name}`, directory: false })
      if (path) onChange(path)
    } catch {
      // cancelled
    }
  }, [mode, ctx, param.name, onChange])

  return (
    <div className="filepick">
      <input
        id={id}
        type="text"
        className="input"
        placeholder={param.default ?? 'choose a path…'}
        readOnly
        value={strVal}
        onChange={() => {
          // readOnly; no-op for React controlled input
        }}
      />
      <button className="btn ghost" type="button" onClick={() => void handlePick()}>
        {mode === 'save' ? 'Save as…' : 'Choose…'}
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// CONTROLS registry
// ---------------------------------------------------------------------------
export const CONTROLS: Record<ControlType | 'ref', ControlComponent> = {
  text: Text,
  textarea: Textarea,
  integer: NumberInput,
  number: NumberInput,
  slider: Slider,
  toggle: Toggle,
  select: Select,
  combobox: Combobox,
  tags: Tags,
  json: JsonEditor,
  file: FileControl,
  ref: Ref,
}

// ---------------------------------------------------------------------------
// pickControl — mirrors buildControl (form-gen.js:45-58)
//
// Priority:
//   file → FileControl
//   select → Select
//   combobox → Combobox
//   toggle → Toggle
//   slider → Slider
//   integer | number → NumberInput
//   tags → Tags (or Ref if refKind)
//   json → JsonEditor
//   textarea → Textarea
//   else refKind → Ref
//   else → Text
// ---------------------------------------------------------------------------
export function pickControl(param: Param): ControlComponent {
  const ctrl = param.control
  if (ctrl === 'file') return FileControl
  if (ctrl === 'select') return Select
  if (ctrl === 'combobox') return Combobox
  if (ctrl === 'toggle') return Toggle
  if (ctrl === 'slider') return Slider
  if (ctrl === 'integer' || ctrl === 'number') return NumberInput
  if (ctrl === 'tags') return param.refKind ? Ref : Tags
  if (ctrl === 'json') return JsonEditor
  if (ctrl === 'textarea') return Textarea
  if (param.refKind) return Ref
  return Text
}

// ---------------------------------------------------------------------------
// coerceValue — parse raw values stored by NumberInput / JsonEditor at
// collect() time. Also trims Text / Textarea / Combobox (mirrors form-gen.js
// trimming at get() time rather than on every keystroke). Throws Error with
// the plain message for collect() to wrap.
// ---------------------------------------------------------------------------
export function coerceValue(param: Param, raw: unknown): unknown {
  if (raw === undefined || raw === null) return undefined

  const ctrl = param.control
  if (ctrl === 'text' || ctrl === 'textarea' || ctrl === 'combobox') {
    if (typeof raw !== 'string') return undefined
    const trimmed = raw.trim()
    return trimmed === '' ? undefined : trimmed
  }
  if (ctrl === 'integer' || ctrl === 'number') {
    const s = String(raw).trim()
    if (s === '') return undefined
    const n = ctrl === 'integer' ? parseInt(s, 10) : parseFloat(s)
    if (Number.isNaN(n)) throw new Error('not a number')
    return n
  }
  if (ctrl === 'json') {
    const s = String(raw).trim()
    if (s === '') return undefined
    try {
      return JSON.parse(s)
    } catch {
      throw new Error('invalid JSON')
    }
  }
  return raw
}
