import { describe, it, expect } from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import * as locator from '../src/main/vouch-locator'

function writeFakeVouch(dir: string, helpText: string): string {
  const script = path.join(dir, 'fake-vouch.js')
  fs.writeFileSync(script, `#!/usr/bin/env node
const help = ${JSON.stringify(helpText)};
const args = process.argv.slice(2);
if (args.length === 1 && args[0] === "--version") { console.log("vouch, version test"); process.exit(0); }
if (args[0] === "review-ui" && args[1] === "--help") { console.log(help); process.exit(0); }
process.exit(2);
`)
  fs.chmodSync(script, 0o755)
  return script
}

describe('vouch-locator', () => {
  it('records sandbox support from a configured path', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vd-loc-'))
    try {
      const script = writeFakeVouch(dir, '--allow-dual-solve\n--dual-solve-sandbox')
      const r = locator.resolveVouch({ configuredPath: script })
      expect(r?.supportsDualSolveSandbox).toBe(true)
    } finally { fs.rmSync(dir, { recursive: true, force: true }) }
  })

  it('prefers repo dev vouch before PATH', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vd-loc-'))
    try {
      const script = writeFakeVouch(dir, '--dual-solve-sandbox')
      const r = locator.resolveVouch({ devVouchPath: script })
      expect(r?.kind).toBe('repo-dev')
      expect(r?.cmd).toBe(script)
    } finally { fs.rmSync(dir, { recursive: true, force: true }) }
  })

  it('keeps the legacy sibling dev fallback', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vd-loc-'))
    try {
      const missingRepoDev = path.join(dir, 'missing-vouch')
      const script = writeFakeVouch(dir, '--dual-solve-sandbox')
      const r = locator.resolveVouch({ devVouchPath: missingRepoDev, siblingDevVouchPath: script })
      expect(r?.kind).toBe('sibling-dev')
      expect(r?.cmd).toBe(script)
    } finally { fs.rmSync(dir, { recursive: true, force: true }) }
  })

  it('normalizeKbRoot maps a .vouch dir to its project root', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'vd-root-'))
    try {
      const kb = path.join(root, '.vouch'); fs.mkdirSync(kb)
      expect(locator.normalizeKbRoot(kb)).toBe(root)
      expect(locator.normalizeKbRoot(root)).toBe(root)
      expect(locator.hasKb(kb)).toBe(true)
    } finally { fs.rmSync(root, { recursive: true, force: true }) }
  })
})
