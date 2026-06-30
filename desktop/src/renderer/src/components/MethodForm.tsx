// MethodForm.tsx — a form for a single Method, exposing collect() via ref.
//
// Ported from src/renderer/lib/form-gen.js:6-43 (buildForm/collect).
// collect() rules are VERBATIM from form-gen.js:26-40.

import { forwardRef, useImperativeHandle, useRef, useState } from 'react'
import type { Method, Param } from '../../../shared/methods.types'
import { pickControl, coerceValue, type FormCtx } from './controls/index'

// ---------------------------------------------------------------------------
// Public handle type — exposed via ref
// ---------------------------------------------------------------------------
export interface MethodFormHandle {
  collect: () => Record<string, unknown>
}

// ---------------------------------------------------------------------------
// MethodForm
// ---------------------------------------------------------------------------
interface MethodFormProps {
  method: Method
  ctx: FormCtx
}

// ---------------------------------------------------------------------------
// getSliderDefault / getToggleDefault — mirror sliderControl/toggleControl
// default logic so we can eagerly populate valuesRef at render time, closing
// the useEffect timing window (fixes the "untouched slider/toggle undefined"
// bug: valuesRef is pre-seeded so collect() never sees undefined for them).
// ---------------------------------------------------------------------------
function getSliderDefault(p: Param): number {
  const raw = p.default !== '' && p.default != null ? Number(p.default) : 0.7
  return Number.isNaN(raw) ? 0.7 : raw
}

function getToggleDefault(p: Param): boolean {
  return String(p.default).toLowerCase() === 'true'
}

export const MethodForm = forwardRef<MethodFormHandle, MethodFormProps>(
  function MethodForm({ method, ctx }, ref) {
    const params = method.params ?? []

    // Hold current values for all params, keyed by param name.
    // A ref is used so onChange callbacks are always up to date without
    // causing re-renders for every keystroke.
    //
    // Slider and Toggle params are pre-seeded with their defaults here at
    // render time (not in a useEffect) so collect() never reads undefined
    // for a control the user has never touched — mirroring form-gen.js where
    // range.value / checkbox.checked always hold their default synchronously.
    const initialValues: Record<string, unknown> = {}
    for (const p of params) {
      if (p.control === 'slider') initialValues[p.name] = getSliderDefault(p)
      else if (p.control === 'toggle') initialValues[p.name] = getToggleDefault(p)
      else if (
        p.control === 'select' &&
        p.required &&
        p.enum?.length &&
        !(p.default && p.enum.includes(p.default))
      ) {
        // Required select with no usable default: mirror the original selectControl
        // behaviour where the native select always defaults to its first option.
        initialValues[p.name] = p.enum[0]
      }
    }
    const valuesRef = useRef<Record<string, unknown>>(initialValues)

    // Store params in a ref so collect() always reads the current params even
    // if the parent re-uses this mounted instance with a different method prop.
    // This avoids a stale-closure bug where deps=[] would freeze collect() on
    // the first-render params while valuesRef reflects newer field values.
    const paramsRef = useRef<typeof params>(params)
    paramsRef.current = params

    // Expose collect() to the parent via the forwarded ref.
    // deps=[] is correct here because collect() reads from paramsRef.current
    // (always fresh) and valuesRef.current (always fresh) — no stale closures.
    useImperativeHandle(
      ref,
      () => ({
        collect(): Record<string, unknown> {
          const out: Record<string, unknown> = {}
          const missing: string[] = []
          for (const p of paramsRef.current) {
            let val: unknown
            try {
              const raw = valuesRef.current[p.name]
              val = coerceValue(p, raw)
            } catch (e) {
              throw new Error(`${p.name}: ${(e as Error).message}`)
            }
            if (
              val === undefined ||
              val === '' ||
              (Array.isArray(val) && val.length === 0)
            ) {
              if (p.required) missing.push(p.name)
              continue
            }
            out[p.name] = val
          }
          if (missing.length) throw new Error(`required: ${missing.join(', ')}`)
          return out
        },
      }),
      [],
    )

    // One local state object to hold display values (controlled inputs need
    // React state so they re-render when the user types).
    const [displayValues, setDisplayValues] =
      useFormValues(params.map((p) => p.name))

    function handleChange(name: string, v: unknown) {
      // Keep the ref in sync (used by collect()).
      valuesRef.current[name] = v
      // Keep display state in sync (drives controlled inputs).
      setDisplayValues(name, v)
    }

    if (params.length === 0) {
      return (
        <div className="form">
          <p className="muted small">no parameters</p>
        </div>
      )
    }

    return (
      <div className="form">
        {params.map((p) => {
          const id = `f-${method.name}-${p.name}`
          const Control = pickControl(p)
          // For slider/toggle, seed the display value with the same default
          // used in valuesRef so the control renders correctly on first paint.
          const rawDisplay = displayValues[p.name]
          const displayVal =
            rawDisplay === undefined && p.control === 'slider'
              ? getSliderDefault(p)
              : rawDisplay === undefined && p.control === 'toggle'
                ? getToggleDefault(p)
                : rawDisplay
          return (
            <div key={p.name} className="field">
              <label htmlFor={id} className="field-label">
                {p.name}
                {p.required && (
                  <span className="req" title="required">
                    {' *'}
                  </span>
                )}
                <span className="ptype">{` ${p.type}`}</span>
              </label>
              <Control
                param={p}
                value={displayVal}
                onChange={(v) => handleChange(p.name, v)}
                ctx={ctx}
                id={id}
              />
              {p.description && (
                <div className="field-help">{p.description}</div>
              )}
            </div>
          )
        })}
      </div>
    )
  },
)

// ---------------------------------------------------------------------------
// useFormValues — tiny hook managing a Record<string, unknown> in state
// so we avoid creating a useState per param.
// ---------------------------------------------------------------------------
function useFormValues(names: string[]) {
  const [values, setValues] = useState<Record<string, unknown>>(() => {
    const init: Record<string, unknown> = {}
    for (const n of names) init[n] = undefined
    return init
  })

  function set(name: string, v: unknown) {
    setValues((prev) => ({ ...prev, [name]: v }))
  }

  return [values, set] as const
}
