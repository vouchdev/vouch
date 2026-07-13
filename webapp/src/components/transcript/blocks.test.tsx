import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { DiffView } from './DiffView'
import { ThinkingBlock } from './ThinkingBlock'

describe('DiffView', () => {
  it('tags added and removed lines', () => {
    const { container } = render(<DiffView text={'@@ -1 +1 @@\n-old\n+new\n ctx'} />)
    expect(container.querySelector('.diff-add')?.textContent).toContain('+new')
    expect(container.querySelector('.diff-del')?.textContent).toContain('-old')
    expect(container.querySelector('.diff-hunk')?.textContent).toContain('@@')
  })
})

describe('ThinkingBlock', () => {
  it('is collapsed by default and expands on click', async () => {
    render(<ThinkingBlock text="secret reasoning" />)
    expect(screen.queryByText('secret reasoning')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: /thinking/i }))
    expect(screen.getByText('secret reasoning')).toBeInTheDocument()
  })
})
