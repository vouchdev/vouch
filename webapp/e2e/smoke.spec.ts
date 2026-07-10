import { expect, test } from '@playwright/test'

const ENDPOINT = 'http://127.0.0.1:8971'

test('connect → ask → citation drawer → review approve', async ({ page }) => {
  await page.goto('/')

  // Connect dialog
  await page.getByLabel(/endpoint/i).fill(ENDPOINT)
  await page.getByRole('button', { name: /connect/i }).click()
  await expect(page.getByText('127.0.0.1:8971')).toBeVisible()

  // / lands on the Dashboard; move to Chat for the ask flow
  await expect(page.getByRole('heading', { name: /dashboard — kb activity/i })).toBeVisible()
  await page.getByRole('link', { name: /chat/i }).click()

  // Chat: cited answer from the approved claim
  await page.getByPlaceholder(/ask the kb/i).fill('what does the vouch http server bind')
  await page.keyboard.press('Enter')
  await expect(page.getByText(/binds 127\.0\.0\.1:8731 by default/)).toBeVisible()

  // Citation chip opens the claim drawer
  await page.getByRole('button', { name: /the-vouch-http-server-binds/ }).first().click()
  await expect(page.getByTestId('drawer')).toBeVisible()
  await page.getByRole('button', { name: /close/i }).click()

  // Pending: the pending claim is in the queue; approve it
  await page.getByRole('link', { name: /pending/i }).click()
  await page.getByText(/review queue holds proposals/i).click()
  await page.getByRole('button', { name: /approve/i }).click()
  await expect(page.getByText(/queue is clear/i)).toBeVisible()
})
