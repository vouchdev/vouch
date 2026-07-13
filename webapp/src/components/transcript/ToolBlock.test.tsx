import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { TranscriptBlock } from '../../lib/transcript'
import { ToolBlock } from './ToolBlock'

type ToolUse = Extract<TranscriptBlock, { type: 'tool_use' }>

function tool(over: Partial<ToolUse>): ToolUse {
  return { type: 'tool_use', id: 't1', name: 'Bash', input: {}, result: null, ...over }
}

describe('ToolBlock', () => {
  it('shows the tool name and a Bash command header', () => {
    render(<ToolBlock block={tool({ name: 'Bash', input: { command: 'go test ./...' } })} />)
    expect(screen.getByText('Bash')).toBeInTheDocument()
    expect(screen.getByText('go test ./...')).toBeInTheDocument()
  })

  it('renders a diff for Edit results and reveals output on expand', async () => {
    const block = tool({
      name: 'Edit',
      input: { file_path: '/x.go' },
      result: { content: '@@ -1 +1 @@\n-a\n+b', is_error: false, subagent_session_id: null },
    })
    const { container } = render(<ToolBlock block={block} />)
    await userEvent.click(screen.getByRole('button', { name: /Edit/i }))
    expect(container.querySelector('.diff-add')).not.toBeNull()
  })

  it('marks errored results', async () => {
    const block = tool({ result: { content: 'boom', is_error: true, subagent_session_id: null } })
    render(<ToolBlock block={block} />)
    await userEvent.click(screen.getByRole('button', { name: /Bash/i }))
    expect(screen.getByText('boom')).toBeInTheDocument()
    expect(screen.getByTestId('tool-error')).toBeInTheDocument()
  })

  it('offers a subagent link and fires the callback', async () => {
    const onOpen = vi.fn()
    const block = tool({
      name: 'Task',
      input: { subagent_type: 'Explore', prompt: 'find x' },
      result: { content: 'done', is_error: false, subagent_session_id: 'child-9' },
    })
    render(<ToolBlock block={block} onOpenSubagent={onOpen} />)
    await userEvent.click(screen.getByRole('button', { name: /Task/i }))
    await userEvent.click(screen.getByRole('button', { name: /view subagent/i }))
    expect(onOpen).toHaveBeenCalledWith('child-9')
  })
})
