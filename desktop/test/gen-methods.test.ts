import { describe, it, expect } from 'vitest'
import { enrich } from '../scripts/gen-methods'
import raw from '../src/catalog/methods.json'

describe('gen-methods enrich', () => {
  const methods = enrich(raw as any)
  it('keeps 54 methods', () => expect(methods.length).toBe(54))
  it('maps kb.trace keys to from/to', () => {
    const t = methods.find(m => m.name === 'kb.trace')!
    const keys = (t.params || []).map(p => p.name)
    expect(keys).toContain('from'); expect(keys).toContain('to')
    expect(keys).not.toContain('from_id')
  })
  it('wires evidence as a multi source ref', () => {
    const pc = methods.find(m => m.name === 'kb.propose_claim')!
    const ev = pc.params!.find(p => p.name === 'evidence')!
    expect(ev.refKind).toBe('source'); expect(ev.refMulti).toBe(true)
  })
  it('gives every select/combobox param an enum', () => {
    for (const m of methods) for (const p of m.params || [])
      if (p.control === 'select' || p.control === 'combobox')
        expect(p.enum && p.enum.length).toBeTruthy()
  })
})
