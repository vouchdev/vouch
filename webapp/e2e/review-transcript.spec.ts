import { expect, test } from '@playwright/test'

// Drives the real frontend (routing, ReviewView, TranscriptView, block
// renderers) against stubbed /proxy responses, so it is independent of the
// backend build. The connection is seeded into localStorage; health and
// capabilities are stubbed so the project comes up "ok" with the transcript
// method advertised, then list_sessions + session_transcript are served.
const ENDPOINT = 'http://127.0.0.1:8971'
const CAPS = {
  name: 'vouch',
  version: '1.2.2',
  level: 3,
  methods: ['kb.list_sessions', 'kb.summarize_session', 'kb.session_transcript'],
  review_gated: true,
}

test('review tab renders a picked session transcript', async ({ page }) => {
  await page.addInitScript((endpoint) => {
    localStorage.setItem(
      'vouch-ui.connections.v2',
      JSON.stringify({ projects: [{ endpoint }], scope: 'all' }),
    )
  }, ENDPOINT)

  await page.route('**/proxy/health', (route) => route.fulfill({ json: { ok: true } }))
  await page.route('**/proxy/capabilities', (route) => route.fulfill({ json: CAPS }))
  await page.route('**/proxy/rpc', async (route) => {
    const body = route.request().postDataJSON() as { method: string }
    if (body.method === 'kb.list_pending') {
      // Shell fans this out for its nav badge; it expects an array.
      return route.fulfill({ json: { ok: true, id: '1', result: [] } })
    }
    if (body.method === 'kb.list_sessions') {
      return route.fulfill({
        json: {
          ok: true,
          id: '1',
          result: {
            sessions: [
              {
                session_id: 'e2e-sid',
                stage: 'buffer',
                proposal_id: null,
                kind: null,
                title: 'e2e session',
                summarized: false,
                observations: 2,
                last_activity: '2026-07-10T00:00:00Z',
              },
            ],
          },
        },
      })
    }
    if (body.method === 'kb.session_transcript') {
      return route.fulfill({
        json: {
          ok: true,
          id: '1',
          result: {
            available: true,
            source: { agent: 'claude', path: '/x' },
            session: {
              id: 'e2e-sid',
              agent: 'claude',
              cwd: '/repo',
              git_branch: 'main',
              title: 'e2e session',
              started_at: null,
              ended_at: null,
              model: 'claude-opus-4-8',
              tokens: { input: 1, output: 1, cache_read: 0, cache_creation: 0 },
            },
            messages: [
              {
                role: 'assistant',
                id: 'm',
                model: 'claude-opus-4-8',
                timestamp: null,
                tokens: null,
                blocks: [{ type: 'text', text: 'e2e transcript body' }],
              },
            ],
            truncated: false,
          },
        },
      })
    }
    return route.fulfill({ json: { ok: true, id: '1', result: {} } })
  })

  await page.goto('/review')
  await page.getByText('e2e session').click()
  await expect(page.getByText('e2e transcript body')).toBeVisible()
})
