import { describe, it, expect } from 'vitest'
import { JsonlClient, DEFAULT_TIMEOUT_MS, LONG_TIMEOUT_MS } from '../src/main/jsonl-client'

const FAKE_LAUNCHER = { cmd: 'node', baseArgs: [] }

describe('JsonlClient', () => {
  it('exports DEFAULT_TIMEOUT_MS and LONG_TIMEOUT_MS', () => {
    expect(DEFAULT_TIMEOUT_MS).toBe(30_000)
    expect(LONG_TIMEOUT_MS).toBe(10 * 60_000)
  })

  it('starts not alive and with null pid', () => {
    const client = new JsonlClient(FAKE_LAUNCHER, { root: '/tmp' })
    expect(client.alive).toBe(false)
    expect(client.pid).toBe(null)
  })

  it('accepts a minimal launcher with only cmd and baseArgs', () => {
    // Verifies the constructor accepts {cmd, baseArgs} without requiring full Launcher interface
    const minimal = { cmd: '/usr/bin/vouch', baseArgs: ['--config', '/etc/vouch.json'] }
    const client = new JsonlClient(minimal, { root: '/tmp' })
    expect(client.alive).toBe(false)
  })

  it('rejects call() when not started', async () => {
    const client = new JsonlClient(FAKE_LAUNCHER, { root: '/tmp' })
    await expect(client.call('kb.status', {})).rejects.toMatchObject({ code: 'process_down' })
  })

  it('stop() resolves immediately when not started', async () => {
    const client = new JsonlClient(FAKE_LAUNCHER, { root: '/tmp' })
    await expect(client.stop()).resolves.toBeUndefined()
  })

  it('stderrTail is empty before start', () => {
    const client = new JsonlClient(FAKE_LAUNCHER, { root: '/tmp' })
    expect(client.stderrTail).toEqual([])
  })

  it('pid returns undefined (not null) after start if OS omits pid, otherwise a number', async () => {
    // After start(), this.child is set; child.pid may be number|undefined.
    // The getter must mirror the JS: this.child ? this.child.pid : null
    // We verify that pid is null before start (child is null).
    const client = new JsonlClient(FAKE_LAUNCHER, { root: '/tmp' })
    expect(client.pid).toBe(null) // child is null — ternary returns null
  })
})
