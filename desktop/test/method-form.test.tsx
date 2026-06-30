import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, within, cleanup } from '@testing-library/react'
import { createRef } from 'react'
import { MethodForm, type MethodFormHandle } from '../src/renderer/src/components/MethodForm'
import type { Method } from '../src/shared/methods.types'

const ctx = { search: async () => [], pickFile: async () => null, pickSave: async () => null }

const method: Method = {
  name: 'kb.demo', view: 'browse',
  params: [
    { name: 'text', type: 'string', required: true, control: 'textarea' },
    { name: 'limit', type: 'integer', control: 'integer' },
    { name: 'tags', type: 'array<string>', control: 'tags' },
  ],
}

// Unmount between tests so queries don't bleed across renders.
afterEach(cleanup)

describe('MethodForm.collect', () => {
  it('drops empty optionals and coerces types', () => {
    const ref = createRef<MethodFormHandle>()
    const { container } = render(<MethodForm ref={ref} method={method} ctx={ctx} />)
    const form = within(container)
    fireEvent.change(form.getByLabelText(/^text/i), { target: { value: 'hello' } })
    fireEvent.change(form.getByLabelText(/^limit/i), { target: { value: '5' } })
    expect(ref.current!.collect()).toEqual({ text: 'hello', limit: 5 })
  })

  it('throws listing missing required fields', () => {
    const ref = createRef<MethodFormHandle>()
    render(<MethodForm ref={ref} method={method} ctx={ctx} />)
    expect(() => ref.current!.collect()).toThrow(/required: text/)
  })

  it('wraps per-field parse errors as "<param>: <msg>" (form-gen.js:31)', () => {
    // Use a JSON param — textarea accepts arbitrary text so JSDOM does not
    // sanitize the value, making it the clearest way to exercise the
    // per-field "<name>: <msg>" error-wrapping path (form-gen.js:31).
    const jsonMethod: Method = {
      name: 'kb.json', view: 'browse',
      params: [
        { name: 'opts', type: 'object', required: true, control: 'json' },
      ],
    }
    const ref = createRef<MethodFormHandle>()
    const { container } = render(<MethodForm ref={ref} method={jsonMethod} ctx={ctx} />)
    const form = within(container)
    // Type invalid JSON into the textarea
    fireEvent.change(form.getByLabelText(/^opts/i), { target: { value: 'not json' } })
    expect(() => ref.current!.collect()).toThrow('opts: invalid JSON')
  })

  it('includes untouched slider default in collect() output (form-gen.js sliderControl always-present get)', () => {
    const sliderMethod: Method = {
      name: 'kb.slider', view: 'browse',
      params: [
        { name: 'score', type: 'number', control: 'slider', default: '0.7' },
      ],
    }
    const ref = createRef<MethodFormHandle>()
    render(<MethodForm ref={ref} method={sliderMethod} ctx={ctx} />)
    // Never touch the slider — collect() must still return the default
    const out = ref.current!.collect()
    expect(out).toEqual({ score: 0.7 })
  })

  it('includes untouched toggle default (false) in collect() output (form-gen.js toggleControl always-present get)', () => {
    const toggleMethod: Method = {
      name: 'kb.toggle', view: 'browse',
      params: [
        { name: 'verbose', type: 'boolean', control: 'toggle', default: 'false' },
      ],
    }
    const ref = createRef<MethodFormHandle>()
    render(<MethodForm ref={ref} method={toggleMethod} ctx={ctx} />)
    // Never touch the toggle — collect() must still return the default boolean
    const out = ref.current!.collect()
    expect(out).toEqual({ verbose: false })
  })

  it('includes untouched toggle default (true) in collect() output', () => {
    const toggleMethod: Method = {
      name: 'kb.toggle2', view: 'browse',
      params: [
        { name: 'enabled', type: 'boolean', control: 'toggle', default: 'true' },
      ],
    }
    const ref = createRef<MethodFormHandle>()
    render(<MethodForm ref={ref} method={toggleMethod} ctx={ctx} />)
    const out = ref.current!.collect()
    expect(out).toEqual({ enabled: true })
  })

  it('collects non-empty tags as string[] (exercises Array.isArray branch in collect())', () => {
    const ref = createRef<MethodFormHandle>()
    const { container } = render(<MethodForm ref={ref} method={method} ctx={ctx} />)
    const form = within(container)
    fireEvent.change(form.getByLabelText(/^text/i), { target: { value: 'hello' } })
    // Type a tag and commit with Enter
    const tagsInput = form.getByPlaceholderText(/type \+ Enter/i)
    fireEvent.change(tagsInput, { target: { value: 'alpha' } })
    fireEvent.keyDown(tagsInput, { key: 'Enter' })
    const out = ref.current!.collect()
    expect(out).toEqual({ text: 'hello', tags: ['alpha'] })
  })

  it('untouched required select submits enum[0] (mirrors original selectControl behaviour)', () => {
    // kb.propose_entity.entity_type is required with no default — the original
    // selectControl defaulted to its first native option ('person').
    const selectMethod: Method = {
      name: 'kb.propose_entity', view: 'propose',
      params: [
        { name: 'name', type: 'string', required: true, control: 'text' },
        {
          name: 'entity_type', type: 'string', required: true, control: 'select',
          enum: ['person', 'project', 'repo'],
        },
      ],
    }
    const ref = createRef<MethodFormHandle>()
    const { container } = render(<MethodForm ref={ref} method={selectMethod} ctx={ctx} />)
    const form = within(container)
    fireEvent.change(form.getByLabelText(/^name/i), { target: { value: 'Alice' } })
    // entity_type is never touched — collect() must return enum[0] ('person')
    const out = ref.current!.collect()
    expect(out).toEqual({ name: 'Alice', entity_type: 'person' })
  })

  it('untouched non-required select is absent from collect() output', () => {
    const selectMethod: Method = {
      name: 'kb.list_claims', view: 'browse',
      params: [
        {
          name: 'status', type: 'string', required: false, control: 'select',
          enum: ['working', 'stable'],
        },
      ],
    }
    const ref = createRef<MethodFormHandle>()
    render(<MethodForm ref={ref} method={selectMethod} ctx={ctx} />)
    // Never touch status — collect() must omit it (non-required, undefined)
    const out = ref.current!.collect()
    expect(out).toEqual({})
  })
})
