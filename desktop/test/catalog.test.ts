import { describe, it, expect } from 'vitest'
import { methods } from '../src/shared/methods.gen'

const VIEWS = new Set(['dashboard','search','browse','propose','review',
  'sessions','graph','maintenance','export-import','audit'])

describe('catalog', () => {
  it('has 54 uniquely-named kb.* methods', () => {
    expect(methods.length).toBe(54)
    const names = methods.map(m => m.name)
    expect(new Set(names).size).toBe(54)
    expect(names.every(n => n.startsWith('kb.'))).toBe(true)
  })
  it('assigns every method a known view', () => {
    for (const m of methods) expect(VIEWS.has(m.view)).toBe(true)
  })
  it('every select/combobox param has a populated enum', () => {
    for (const m of methods) for (const p of m.params || [])
      if (p.control === 'select' || p.control === 'combobox')
        expect(Array.isArray(p.enum) && p.enum!.length > 0).toBe(true)
  })
  it('kb.trace uses from/to wire keys', () => {
    const t = methods.find(m => m.name === 'kb.trace')!
    const keys = t.params!.map(p => p.name)
    expect(keys.includes('from') && keys.includes('to')).toBe(true)
    expect(keys.includes('from_id') || keys.includes('to_id')).toBe(false)
  })
  it('ref/file params carry refKind/file', () => {
    const byName = (n: string) => methods.find(m => m.name === n)!
    expect(byName('kb.propose_claim').params!.find(p => p.name === 'evidence')!.refKind).toBe('source')
    expect(byName('kb.export').params!.find(p => p.name === 'out_path')!.file).toBe('save')
    expect(byName('kb.import_apply').params!.find(p => p.name === 'bundle_path')!.file).toBe('open')
  })
  it('gated methods also mutate', () => {
    for (const m of methods) if (m.gated) expect(m.mutates).toBe(true)
  })
})
