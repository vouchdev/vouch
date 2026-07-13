import { execFileSync, spawn } from 'node:child_process'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

export const VOUCH_PORT = 8971
const STATE_FILE = join(process.cwd(), 'e2e', '.kb-state.json')

function vouch(cwd: string, ...args: string[]): string {
  return execFileSync('vouch', args, { cwd, encoding: 'utf-8' }).trim()
}

async function waitForHealth(url: string, tries = 50): Promise<void> {
  for (let i = 0; i < tries; i++) {
    try {
      const res = await fetch(url)
      if (res.ok) return
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 200))
  }
  throw new Error(`vouch serve did not become healthy at ${url}`)
}

export default async function globalSetup(): Promise<void> {
  try {
    execFileSync('vouch', ['--version'], { encoding: 'utf-8' })
  } catch {
    throw new Error('the e2e suite needs the `vouch` CLI on PATH (pipx install vouch)')
  }

  const dir = mkdtempSync(join(tmpdir(), 'vouch-ui-e2e-'))
  vouch(dir, 'init')
  // The UI approves as a different actor than the proposer, but keep the
  // guard off so the seed script itself can approve its own claim. `vouch
  // init` already writes a `review:` section — insert the key inside it
  // rather than appending a duplicate top-level `review:` mapping.
  const cfgPath = join(dir, '.vouch', 'config.yaml')
  const cfg = readFileSync(cfgPath, 'utf-8')
  writeFileSync(
    cfgPath,
    cfg.includes('review:')
      ? cfg.replace(/^review:$/m, 'review:\n  approver_role: trusted-agent')
      : `${cfg}\nreview:\n  approver_role: trusted-agent\n`,
  )

  writeFileSync(join(dir, 'note.txt'), 'The vouch HTTP server binds 127.0.0.1:8731 by default.\n')
  const sourceOut = vouch(dir, 'source', 'add', 'note.txt')
  const sourceId = sourceOut.split('\n').at(-1)!.trim()

  // Claim 1 — approved, so chat has something to synthesize.
  const prop1 = vouch(
    dir, 'propose-claim',
    '--text', 'The vouch HTTP server binds 127.0.0.1:8731 by default',
    '--source', sourceId,
  ).split('\n').at(-1)!.trim()
  vouch(dir, 'approve', prop1)

  // Claim 2 — left pending, so the Review view has a queue item.
  vouch(
    dir, 'propose-claim',
    '--text', 'The vouch review queue holds proposals until a reviewer decides',
    '--source', sourceId,
  )

  const server = spawn('vouch', ['serve', '--transport', 'http', '--port', String(VOUCH_PORT)], {
    cwd: dir,
    detached: true,
    stdio: 'ignore',
  })
  server.unref()

  writeFileSync(STATE_FILE, JSON.stringify({ dir, pid: server.pid }))
  await waitForHealth(`http://127.0.0.1:${VOUCH_PORT}/health`)
}
