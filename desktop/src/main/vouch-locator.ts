/*
 * vouch-locator.ts — resolve a runnable `vouch`, validate it, and run the one
 * sanctioned CLI subprocess (`vouch init`). Resolution order (first hit wins):
 *   1. user-configured absolute path (settings / first-run wizard)
 *   2. env override VOUCH_DESKTOP_VOUCH_PATH
 *   3. bundled frozen vouch under resources/vouch/ (default shipped flavor)
 *   4. monorepo dev checkout .venv/bin/vouch
 *   5. sibling dev checkout ../vouch/.venv/bin/vouch
 *   6. `vouch` on PATH
 *   7. `python3 -m vouch`
 * Every candidate is validated by spawning `<vouch> --version`.
 *
 * A launcher is {cmd, baseArgs}: the command plus the args that must precede the
 * subcommand (empty for a real binary, ["-m","vouch"] for the module form).
 */
import { spawn, spawnSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'
import * as os from 'node:os'

export interface Launcher {
  cmd: string
  baseArgs: string[]
  kind: string
  version: string
  supportsDualSolveSandbox: boolean
}

export const DEFAULT_SANDBOX_IMAGE = 'vouch/coder:latest'

function probe(cmd: string, baseArgs: string[]): string | null {
  try {
    const r = spawnSync(cmd, [...baseArgs, '--version'], {
      encoding: 'utf8',
      timeout: 8000,
    })
    if (r.status === 0 && /vouch/i.test((r.stdout || '') + (r.stderr || ''))) {
      return ((r.stdout || r.stderr) + '').trim()
    }
  } catch { /* not runnable */ }
  return null
}

export function supportsDualSolveSandbox(launcher: { cmd: string; baseArgs: string[] } | null): boolean {
  if (!launcher) return false
  try {
    const r = spawnSync(launcher.cmd, [...launcher.baseArgs, 'review-ui', '--help'], {
      encoding: 'utf8',
      timeout: 8000,
    })
    return r.status === 0 && /--dual-solve-sandbox/.test((r.stdout || '') + (r.stderr || ''))
  } catch {
    return false
  }
}

/**
 * @param opts - configuredPath, bundledDir, devVouchPath, siblingDevVouchPath
 * @returns Launcher or null
 */
export function resolveVouch(opts: {
  configuredPath?: string;
  bundledDir?: string;
  devVouchPath?: string;
  siblingDevVouchPath?: string;
} = {}): Launcher | null {
  const candidates: { cmd: string; baseArgs: string[]; kind: string }[] = []
  if (opts.configuredPath) candidates.push({ cmd: opts.configuredPath, baseArgs: [], kind: 'configured' })
  if (process.env.VOUCH_DESKTOP_VOUCH_PATH) {
    candidates.push({ cmd: process.env.VOUCH_DESKTOP_VOUCH_PATH, baseArgs: [], kind: 'env' })
  }
  if (opts.bundledDir) {
    const exe = process.platform === 'win32' ? 'vouch.exe' : 'vouch'
    const p = path.join(opts.bundledDir, exe)
    if (fs.existsSync(p)) candidates.push({ cmd: p, baseArgs: [], kind: 'bundled' })
  }

  const venvBin = process.platform === 'win32' ? 'Scripts' : 'bin'
  const vouchExe = process.platform === 'win32' ? 'vouch.exe' : 'vouch'
  const devVouchPath = opts.devVouchPath || path.resolve(__dirname, '..', '..', '..', '.venv', venvBin, vouchExe)
  if (fs.existsSync(devVouchPath)) {
    candidates.push({ cmd: devVouchPath, baseArgs: [], kind: 'repo-dev' })
  }
  const siblingDevVouchPath = opts.siblingDevVouchPath || path.resolve(__dirname, '..', '..', '..', 'vouch', '.venv', venvBin, vouchExe)
  if (siblingDevVouchPath !== devVouchPath && fs.existsSync(siblingDevVouchPath)) {
    candidates.push({ cmd: siblingDevVouchPath, baseArgs: [], kind: 'sibling-dev' })
  }
  candidates.push({ cmd: process.platform === 'win32' ? 'vouch.exe' : 'vouch', baseArgs: [], kind: 'path' })
  for (const py of ['python3', 'python']) {
    candidates.push({ cmd: py, baseArgs: ['-m', 'vouch'], kind: 'module' })
  }
  for (const c of candidates) {
    const version = probe(c.cmd, c.baseArgs)
    if (version) return {
      ...c,
      version,
      supportsDualSolveSandbox: supportsDualSolveSandbox(c),
    }
  }
  return null
}

/** Is `dir` (or an ancestor) a git work tree? Gates the dual-solve view. */
export function isGitRepo(dir: string): boolean {
  try {
    const r = spawnSync('git', ['-C', dir, 'rev-parse', '--is-inside-work-tree'], {
      encoding: 'utf8',
      timeout: 5000,
    })
    return r.status === 0 && /true/.test(r.stdout || '')
  } catch {
    return false
  }
}

export function normalizeKbRoot(root: string): string {
  if (!root) return root
  const resolved = path.resolve(root)
  return path.basename(resolved) === '.vouch' ? path.dirname(resolved) : resolved
}

/** Are required dual-solve tools available? Sandbox mode uses Docker for agents. */
export function dualSolveTools(opts: { sandbox?: boolean; image?: string } = {}): { ok: boolean; missing: string[]; sandbox: boolean; image: string } {
  const sandbox = opts.sandbox !== false
  const image = opts.image || DEFAULT_SANDBOX_IMAGE
  const need = sandbox ? ['git', 'gh', 'docker'] : ['git', 'gh', 'claude', 'codex']
  const missing: string[] = []
  for (const t of need) {
    const r = spawnSync(process.platform === 'win32' ? 'where' : 'which', [t], { encoding: 'utf8' })
    if (r.status !== 0) missing.push(t)
  }
  if (sandbox && !missing.includes('docker')) {
    const r = spawnSync('docker', ['image', 'inspect', image], {
      encoding: 'utf8',
      timeout: 8000,
    })
    if (r.status !== 0) missing.push(`docker image ${image}`)
  }
  return { ok: missing.length === 0, missing, sandbox, image }
}

/** Does `<root>/.vouch` exist? */
export function hasKb(root: string): boolean {
  root = normalizeKbRoot(root)
  try {
    return fs.statSync(path.join(root, '.vouch')).isDirectory()
  } catch {
    return false
  }
}

/** One-shot `vouch init <root>` — the only CLI subprocess we use. */
export function initKb(launcher: Launcher, root: string): Promise<{ ok: boolean; code?: number | null; error?: string | null }> {
  root = normalizeKbRoot(root)
  return new Promise((resolve) => {
    const child = spawn(launcher.cmd, [...launcher.baseArgs, 'init', '--path', root], {
      cwd: root,
      env: process.env,
    })
    let err = ''
    child.stderr.on('data', (d: Buffer) => (err += d))
    child.on('error', (e: Error) => resolve({ ok: false, error: e.message }))
    child.on('exit', (code: number | null) => resolve({ ok: code === 0, code, error: code === 0 ? null : err.trim() }))
  })
}

export function defaultAgent(): string {
  return `vouch-desktop:${os.userInfo().username || 'user'}`
}
