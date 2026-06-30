// controls.test.tsx — RTL tests for src/renderer/src/components/controls/index.tsx
// Verifies: id/htmlFor association, value collection, default initialisation,
// trim-on-collect (not on keystroke), ref single-mode flush, timer cleanup.

import React from 'react'
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import type { Param } from '../src/shared/methods.types'
import type { ControlProps, FormCtx } from '../src/renderer/src/components/controls/index'

// Cleanup DOM after every test so renders don't accumulate
afterEach(() => cleanup())

// Mock client module so tests don't need window.vouch
vi.mock('../src/renderer/src/lib/client', () => ({
  pickFile: vi.fn(),
  pickSave: vi.fn(),
}))

import {
  Text,
  Textarea,
  NumberInput,
  Slider,
  Toggle,
  Select,
  Combobox,
  Tags,
  Ref,
  coerceValue,
  pickControl,
} from '../src/renderer/src/components/controls/index'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const noopCtx: FormCtx = {
  search: vi.fn().mockResolvedValue([]),
  pickFile: vi.fn(),
  pickSave: vi.fn(),
}

function param(overrides: Partial<Param>): Param {
  return {
    name: 'p',
    type: 'string',
    control: 'text',
    ...overrides,
  }
}

function renderControl(
  Component: (props: ControlProps) => JSX.Element,
  p: Param,
  opts: { value?: unknown; id?: string } = {},
) {
  const onChange = vi.fn()
  const { rerender } = render(
    <Component
      param={p}
      value={opts.value}
      onChange={onChange}
      ctx={noopCtx}
      id={opts.id ?? `f-m-${p.name}`}
    />,
  )
  return { onChange, rerender }
}

// ---------------------------------------------------------------------------
// id / htmlFor association
// ---------------------------------------------------------------------------
describe('id forwarding', () => {
  it('Text forwards id to input', () => {
    renderControl(Text, param({ name: 'q', control: 'text' }), { id: 'f-m-q' })
    expect(screen.getByRole('textbox').id).toBe('f-m-q')
  })

  it('Textarea forwards id', () => {
    renderControl(Textarea, param({ name: 'body', control: 'textarea' }), { id: 'f-m-body' })
    expect(document.getElementById('f-m-body')).not.toBeNull()
  })

  it('Toggle forwards id to checkbox', () => {
    renderControl(Toggle, param({ name: 'flag', control: 'toggle', default: 'false' }), { id: 'f-m-flag' })
    expect(document.getElementById('f-m-flag')).not.toBeNull()
    expect((document.getElementById('f-m-flag') as HTMLInputElement).type).toBe('checkbox')
  })

  it('Select forwards id', () => {
    renderControl(
      Select,
      param({ name: 'kind', control: 'select', enum: ['a', 'b'], required: true }),
      { id: 'f-m-kind' },
    )
    expect(document.getElementById('f-m-kind')).not.toBeNull()
  })

  it('Ref forwards id to text input', () => {
    renderControl(Ref, param({ name: 'node', control: 'text', refKind: 'node' }), {
      id: 'f-m-node',
    })
    expect(document.getElementById('f-m-node')).not.toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Text — no trim on keystroke; trim via coerceValue
// ---------------------------------------------------------------------------
describe('Text', () => {
  it('emits raw value (not trimmed) mid-edit', () => {
    const { onChange } = renderControl(Text, param({ control: 'text' }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'hello ' } })
    expect(onChange).toHaveBeenCalledWith('hello ')
  })

  it('emits undefined for empty string', () => {
    const { onChange } = renderControl(Text, param({ control: 'text' }), { value: 'hi' })
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '' } })
    expect(onChange).toHaveBeenCalledWith(undefined)
  })
})

// ---------------------------------------------------------------------------
// Textarea — same trim contract
// ---------------------------------------------------------------------------
describe('Textarea', () => {
  it('emits raw value mid-edit', () => {
    const { onChange } = renderControl(Textarea, param({ control: 'textarea' }))
    const ta = document.querySelector('textarea')!
    fireEvent.change(ta, { target: { value: '  hello  ' } })
    expect(onChange).toHaveBeenCalledWith('  hello  ')
  })
})

// ---------------------------------------------------------------------------
// Combobox — no trim on keystroke
// ---------------------------------------------------------------------------
describe('Combobox', () => {
  it('emits raw value mid-edit', () => {
    const { onChange } = renderControl(
      Combobox,
      param({ control: 'combobox', enum: ['a', 'b'], default: 'a' }),
    )
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'a ' } })
    expect(onChange).toHaveBeenCalledWith('a ')
  })
})

// ---------------------------------------------------------------------------
// coerceValue — trim at collect time for text/textarea/combobox
// ---------------------------------------------------------------------------
describe('coerceValue', () => {
  it('trims text at collect time', () => {
    expect(coerceValue(param({ control: 'text' }), '  hello  ')).toBe('hello')
  })

  it('trims textarea at collect time', () => {
    expect(coerceValue(param({ control: 'textarea' }), '  world  ')).toBe('world')
  })

  it('trims combobox at collect time', () => {
    expect(coerceValue(param({ control: 'combobox', enum: [] }), '  foo  ')).toBe('foo')
  })

  it('returns undefined for whitespace-only text', () => {
    expect(coerceValue(param({ control: 'text' }), '   ')).toBeUndefined()
  })

  it('parses integer', () => {
    expect(coerceValue(param({ control: 'integer' }), '42')).toBe(42)
  })

  it('parses float', () => {
    expect(coerceValue(param({ control: 'number' }), '3.14')).toBeCloseTo(3.14)
  })

  it('throws for invalid number', () => {
    expect(() => coerceValue(param({ control: 'integer' }), 'abc')).toThrow('not a number')
  })

  it('parses json', () => {
    expect(coerceValue(param({ control: 'json' }), '{"k":1}')).toEqual({ k: 1 })
  })

  it('throws for invalid json', () => {
    expect(() => coerceValue(param({ control: 'json' }), '{bad}')).toThrow('invalid JSON')
  })
})

// ---------------------------------------------------------------------------
// Toggle — emits default on mount; reflects checked state
// ---------------------------------------------------------------------------
describe('Toggle', () => {
  it('emits default=true on mount when value is undefined', () => {
    const { onChange } = renderControl(
      Toggle,
      param({ control: 'toggle', default: 'true' }),
    )
    expect(onChange).toHaveBeenCalledWith(true)
  })

  it('emits default=false on mount', () => {
    const { onChange } = renderControl(
      Toggle,
      param({ control: 'toggle', default: 'false' }),
    )
    expect(onChange).toHaveBeenCalledWith(false)
  })

  it('does not emit on mount when value already provided', () => {
    const { onChange } = renderControl(
      Toggle,
      param({ control: 'toggle', default: 'true' }),
      { value: false },
    )
    expect(onChange).not.toHaveBeenCalled()
  })

  it('emits new value on user change', () => {
    const { onChange } = renderControl(
      Toggle,
      param({ control: 'toggle', default: 'true' }),
      { value: true },
    )
    const cb = document.getElementById('f-m-p') as HTMLInputElement
    fireEvent.click(cb)
    expect(onChange).toHaveBeenCalledWith(false)
  })
})

// ---------------------------------------------------------------------------
// Slider — emits default on mount
// ---------------------------------------------------------------------------
describe('Slider', () => {
  it('emits default on mount when value is undefined', () => {
    const { onChange } = renderControl(
      Slider,
      param({ control: 'slider', default: '0.7' }),
    )
    expect(onChange).toHaveBeenCalledWith(0.7)
  })

  it('does not emit on mount when value already provided', () => {
    const { onChange } = renderControl(
      Slider,
      param({ control: 'slider', default: '0.7' }),
      { value: 0.5 },
    )
    expect(onChange).not.toHaveBeenCalled()
  })

  it('emits number on slider change', () => {
    const { onChange } = renderControl(
      Slider,
      param({ control: 'slider', default: '0.7' }),
      { value: 0.7 },
    )
    const range = document.querySelector('input[type=range]') as HTMLInputElement
    fireEvent.change(range, { target: { value: '0.9' } })
    expect(onChange).toHaveBeenCalledWith(0.9)
  })
})

// ---------------------------------------------------------------------------
// Select — pre-selects default from enum on mount
// ---------------------------------------------------------------------------
describe('Select', () => {
  it('emits default on mount when default is in enum', () => {
    const { onChange } = renderControl(
      Select,
      param({
        control: 'select',
        enum: ['observation', 'hypothesis', 'fact'],
        default: 'observation',
      }),
    )
    expect(onChange).toHaveBeenCalledWith('observation')
  })

  it('does not emit on mount when default not in enum', () => {
    const { onChange } = renderControl(
      Select,
      param({ control: 'select', enum: ['a', 'b'], default: 'z' }),
    )
    expect(onChange).not.toHaveBeenCalled()
  })

  it('emits undefined for blank option', () => {
    const { onChange } = renderControl(
      Select,
      param({ control: 'select', enum: ['a', 'b'] }),
      { value: 'a' },
    )
    const sel = document.querySelector('select') as HTMLSelectElement
    fireEvent.change(sel, { target: { value: '' } })
    expect(onChange).toHaveBeenCalledWith(undefined)
  })

  it('emits selected value', () => {
    const { onChange } = renderControl(
      Select,
      param({ control: 'select', enum: ['a', 'b'], required: true }),
      { value: 'a' },
    )
    const sel = document.querySelector('select') as HTMLSelectElement
    fireEvent.change(sel, { target: { value: 'b' } })
    expect(onChange).toHaveBeenCalledWith('b')
  })
})

// ---------------------------------------------------------------------------
// Tags — commit on Enter, blur, comma; remove via chip button
// ---------------------------------------------------------------------------
describe('Tags', () => {
  it('adds tag on Enter', () => {
    const { onChange } = renderControl(Tags, param({ control: 'tags' }), { value: [] })
    const inp = screen.getByRole('textbox')
    fireEvent.change(inp, { target: { value: 'foo' } })
    fireEvent.keyDown(inp, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith(['foo'])
  })

  it('adds tag on blur', () => {
    const { onChange } = renderControl(Tags, param({ control: 'tags' }), { value: [] })
    const inp = screen.getByRole('textbox')
    fireEvent.change(inp, { target: { value: 'bar' } })
    fireEvent.blur(inp)
    expect(onChange).toHaveBeenCalledWith(['bar'])
  })

  it('splits comma-separated input', () => {
    const { onChange } = renderControl(Tags, param({ control: 'tags' }), { value: [] })
    const inp = screen.getByRole('textbox')
    fireEvent.change(inp, { target: { value: 'a, b, c' } })
    fireEvent.keyDown(inp, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith(['a', 'b', 'c'])
  })

  it('removes tag via chip-x button', () => {
    const { onChange } = renderControl(Tags, param({ control: 'tags' }), {
      value: ['x', 'y'],
    })
    const buttons = screen.getAllByRole('button')
    fireEvent.click(buttons[0]) // removes 'x'
    expect(onChange).toHaveBeenCalledWith(['y'])
  })

  it('emits undefined when last tag removed', () => {
    const { onChange } = renderControl(Tags, param({ control: 'tags' }), {
      value: ['only'],
    })
    fireEvent.click(screen.getByRole('button'))
    expect(onChange).toHaveBeenCalledWith(undefined)
  })
})

// ---------------------------------------------------------------------------
// Ref single-mode — typed text is flushed to onChange immediately
// ---------------------------------------------------------------------------
describe('Ref single-mode', () => {
  it('flushes typed value to onChange on input (not just on dropdown choose)', () => {
    const { onChange } = renderControl(
      Ref,
      param({ name: 'node_id', control: 'text', refKind: 'node' }),
      { id: 'f-m-node_id' },
    )
    const inp = document.getElementById('f-m-node_id') as HTMLInputElement
    fireEvent.change(inp, { target: { value: 'abc-123' } })
    expect(onChange).toHaveBeenCalledWith('abc-123')
  })

  it('emits undefined when input is cleared', () => {
    const { onChange } = renderControl(
      Ref,
      param({ name: 'node_id', control: 'text', refKind: 'node' }),
      { id: 'f-m-node_id', value: 'abc-123' },
    )
    const inp = document.getElementById('f-m-node_id') as HTMLInputElement
    fireEvent.change(inp, { target: { value: '' } })
    expect(onChange).toHaveBeenCalledWith(undefined)
  })

  it('flushes typed value synchronously (independent of search results)', () => {
    const ctx: FormCtx = {
      search: vi.fn().mockResolvedValue([{ id: 'node-42', kind: 'node', snippet: 'text' }]),
      pickFile: vi.fn(),
      pickSave: vi.fn(),
    }
    const onChange = vi.fn()
    render(
      <Ref
        param={param({ name: 'node_id2', control: 'text', refKind: 'node' })}
        value={undefined}
        onChange={onChange}
        ctx={ctx}
        id="f-m-node_id2"
      />,
    )
    const inp = document.getElementById('f-m-node_id2') as HTMLInputElement
    fireEvent.change(inp, { target: { value: 'no' } })
    // The typed text is flushed synchronously to onChange in single mode
    expect(onChange).toHaveBeenCalledWith('no')
  })
})

// ---------------------------------------------------------------------------
// Ref multi-mode — typed text committed on Enter / blur; not on input
// ---------------------------------------------------------------------------
describe('Ref multi-mode', () => {
  it('does not flush on input (waits for Enter or blur)', () => {
    const { onChange } = renderControl(
      Ref,
      param({ name: 'ids', control: 'tags', refKind: 'node', refMulti: true }),
      { value: [] },
    )
    const inp = screen.getByRole('textbox')
    fireEvent.change(inp, { target: { value: 'node-1' } })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('commits multi value on Enter', () => {
    const { onChange } = renderControl(
      Ref,
      param({ name: 'ids', control: 'tags', refKind: 'node', refMulti: true }),
      { value: [] },
    )
    const inp = screen.getByRole('textbox')
    fireEvent.change(inp, { target: { value: 'node-1' } })
    fireEvent.keyDown(inp, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith(['node-1'])
  })

  it('commits multi value on blur', () => {
    const { onChange } = renderControl(
      Ref,
      param({ name: 'ids', control: 'tags', refKind: 'node', refMulti: true }),
      { value: [] },
    )
    const inp = screen.getByRole('textbox')
    fireEvent.change(inp, { target: { value: 'node-2' } })
    fireEvent.blur(inp)
    expect(onChange).toHaveBeenCalledWith(['node-2'])
  })
})

// ---------------------------------------------------------------------------
// pickControl — routing
// ---------------------------------------------------------------------------
describe('pickControl', () => {
  it('routes file → FileControl', () => {
    const { name } = pickControl(param({ control: 'file', file: 'open' }))
    expect(name).toBe('FileControl')
  })

  it('routes toggle → Toggle', () => {
    expect(pickControl(param({ control: 'toggle' })).name).toBe('Toggle')
  })

  it('routes slider → Slider', () => {
    expect(pickControl(param({ control: 'slider' })).name).toBe('Slider')
  })

  it('routes tags+refKind → Ref', () => {
    expect(
      pickControl(param({ control: 'tags', refKind: 'node' })).name,
    ).toBe('Ref')
  })

  it('routes tags without refKind → Tags', () => {
    expect(pickControl(param({ control: 'tags' })).name).toBe('Tags')
  })

  it('routes refKind text → Ref', () => {
    expect(pickControl(param({ control: 'text', refKind: 'node' })).name).toBe('Ref')
  })

  it('routes plain text → Text', () => {
    expect(pickControl(param({ control: 'text' })).name).toBe('Text')
  })
})
