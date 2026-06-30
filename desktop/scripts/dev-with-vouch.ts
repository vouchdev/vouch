import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDir = path.dirname(fileURLToPath(import.meta.url))
const desktopRoot = path.resolve(scriptDir, '..')
const repoRoot = path.resolve(desktopRoot, '..')

const binDir = process.platform === 'win32' ? 'Scripts' : 'bin'
const vouchExe = process.platform === 'win32' ? 'vouch.exe' : 'vouch'
const pythonExe = process.platform === 'win32' ? 'python.exe' : 'python'
const electronViteExe = process.platform === 'win32' ? 'electron-vite.cmd' : 'electron-vite'
const repoVouch = path.join(repoRoot, '.venv', binDir, vouchExe)
const repoPython = path.join(repoRoot, '.venv', binDir, pythonExe)
const electronVite = path.join(desktopRoot, 'node_modules', '.bin', electronViteExe)
const pyproject = path.join(repoRoot, 'pyproject.toml')
const installStamp = path.join(desktopRoot, 'node_modules', '.cache', 'vouch-dev-install.stamp')

type Args = {
  kb?: string
  watch: boolean
  install: boolean
}

const args = parseArgs(process.argv.slice(2))
const env: NodeJS.ProcessEnv = {
  ...process.env,
  VOUCH_DESKTOP_DEV_RELOAD: '1',
  VOUCH_DESKTOP_DISABLE_CHROME_SANDBOX: '1',
  ELECTRON_DISABLE_SANDBOX: '1',
}

if (args.kb) env.VOUCH_DESKTOP_KB = path.resolve(args.kb)

let child: ChildProcess | null = null
let stoppingForRestart = false
let restartQueued = false
let restartTimer: ReturnType<typeof setTimeout> | null = null
let shutdownRequested = false
let reinstallOnNextRestart = false
const watchers = new Map<string, { watcher: fs.FSWatcher; fileNames: Set<string> | null }>()

ensureEditableVouch(args.install)
if (fs.existsSync(repoVouch)) {
  env.VOUCH_DESKTOP_VOUCH_PATH = repoVouch
} else {
  console.warn(`[dev:vouch] ${relative(repoVouch)} was not found; desktop will fall back to PATH/python -m vouch.`)
  console.warn('[dev:vouch] create the repo venv with: python3 -m venv .venv && .venv/bin/python -m pip install -e .')
}
startElectron('initial launch')
if (args.watch) watchVouchSources()

process.on('SIGINT', () => shutdown('SIGINT'))
process.on('SIGTERM', () => shutdown('SIGTERM'))

function parseArgs(raw: string[]): Args {
  const parsed: Args = { watch: true, install: true }
  for (let i = 0; i < raw.length; i++) {
    const arg = raw[i]
    if (arg === '--kb') {
      const value = raw[++i]
      if (!value) throw new Error('--kb requires a path')
      parsed.kb = value
    } else if (arg.startsWith('--kb=')) {
      parsed.kb = arg.slice('--kb='.length)
    } else if (arg === '--no-watch') {
      parsed.watch = false
    } else if (arg === '--no-install') {
      parsed.install = false
    } else if (arg === '--help' || arg === '-h') {
      console.log('usage: npm run dev:vouch -- [--kb /path/to/repo] [--no-watch] [--no-install]')
      process.exit(0)
    } else {
      throw new Error(`unknown argument: ${arg}`)
    }
  }
  return parsed
}

function ensureEditableVouch(install: boolean, opts: { force?: boolean } = {}): void {
  if (!install) return
  if (!fs.existsSync(repoPython)) {
    console.warn(`[dev:vouch] ${relative(repoPython)} was not found; skipping editable vouch install.`)
    console.warn('[dev:vouch] create the repo venv with: python3 -m venv .venv')
    return
  }
  if (!opts.force && fs.existsSync(repoVouch) && !isNewer(pyproject, installStamp)) return

  console.log('[dev:vouch] refreshing editable vouch install')
  const result = spawnSync(repoPython, ['-m', 'pip', 'install', '-e', '.[dev,web]'], {
    cwd: repoRoot,
    env: process.env,
    stdio: 'inherit',
  })
  if (result.status !== 0) {
    throw new Error(`editable vouch install failed with exit code ${result.status}`)
  }
  try {
    fs.mkdirSync(path.dirname(installStamp), { recursive: true })
    fs.writeFileSync(installStamp, new Date().toISOString())
  } catch {
    // A missing stamp only means the next launch may reinstall.
  }
}

function startElectron(reason: string): void {
  console.log(`[dev:vouch] ${reason}`)
  if (env.VOUCH_DESKTOP_VOUCH_PATH) console.log(`[dev:vouch] vouch: ${relative(env.VOUCH_DESKTOP_VOUCH_PATH)}`)
  if (env.VOUCH_DESKTOP_KB) console.log(`[dev:vouch] kb: ${env.VOUCH_DESKTOP_KB}`)

  child = spawn(electronVite, ['dev'], {
    cwd: desktopRoot,
    env,
    stdio: 'inherit',
    detached: process.platform !== 'win32',
  })

  child.on('exit', (code, signal) => {
    child = null
    if (shutdownRequested) return
    if (stoppingForRestart) {
      stoppingForRestart = false
      const queued = restartQueued
      restartQueued = false
      startElectron(queued ? 'restart after queued vouch change' : 'restart after vouch change')
      return
    }
    closeWatchers()
    const exitCode = typeof code === 'number' ? code : signal ? 1 : 0
    process.exit(exitCode)
  })
}

function watchVouchSources(): void {
  const roots = [
    path.join(repoRoot, 'src', 'vouch'),
    path.join(repoRoot, 'templates'),
    path.join(repoRoot, 'schemas'),
    path.join(repoRoot, 'migrations'),
    path.join(repoRoot, 'pyproject.toml'),
  ]
  for (const root of roots) addWatch(root)
  console.log('[dev:vouch] watching vouch sources; Python changes restart Electron and its vouch child.')
}

function addWatch(target: string): void {
  if (!fs.existsSync(target)) return
  const stat = fs.statSync(target)
  if (stat.isDirectory()) {
    for (const entry of fs.readdirSync(target, { withFileTypes: true })) {
      if (shouldSkip(entry.name)) continue
      const childPath = path.join(target, entry.name)
      if (entry.isDirectory()) addWatch(childPath)
    }
  }

  const watchTarget = stat.isDirectory() ? target : path.dirname(target)
  const watchedFileName = stat.isDirectory() ? null : path.basename(target)
  const existing = watchers.get(watchTarget)
  if (existing) {
    if (existing.fileNames !== null && watchedFileName !== null) existing.fileNames.add(watchedFileName)
    else existing.fileNames = null
    return
  }
  try {
    const watcher = fs.watch(watchTarget, (_event, filename) => {
      if (!filename || shouldSkip(String(filename))) return
      const fileName = String(filename)
      const entry = watchers.get(watchTarget)
      if (entry?.fileNames && !entry.fileNames.has(fileName)) return
      const changed = path.join(watchTarget, fileName)
      if (fs.existsSync(changed)) {
        try {
          if (fs.statSync(changed).isDirectory()) addWatch(changed)
        } catch {
          // The path may have disappeared between fs.watch and stat.
        }
      }
      if (!isRelevantChange(changed)) return
      if (changed === pyproject) reinstallOnNextRestart = true
      scheduleRestart(relative(changed))
    })
    watchers.set(watchTarget, {
      watcher,
      fileNames: watchedFileName === null ? null : new Set([watchedFileName]),
    })
  } catch (err) {
    console.warn(`[dev:vouch] could not watch ${relative(watchTarget)}: ${(err as Error).message}`)
  }
}

function scheduleRestart(reason: string): void {
  if (restartTimer) clearTimeout(restartTimer)
  restartTimer = setTimeout(() => {
    restartTimer = null
    restartElectron(`vouch changed: ${reason}`)
  }, 250)
}

function restartElectron(reason: string): void {
  if (reinstallOnNextRestart) {
    try {
      ensureEditableVouch(args.install, { force: true })
      reinstallOnNextRestart = false
    } catch (err) {
      console.error(`[dev:vouch] ${(err as Error).message}`)
      return
    }
  }
  if (!child) {
    startElectron(reason)
    return
  }
  if (stoppingForRestart) {
    restartQueued = true
    return
  }
  console.log(`[dev:vouch] ${reason}; restarting Electron so vouch reloads.`)
  stoppingForRestart = true
  killChild('SIGTERM')
  setTimeout(() => {
    if (child && stoppingForRestart) killChild('SIGKILL')
  }, 5000)
}

function shutdown(signal: NodeJS.Signals): void {
  shutdownRequested = true
  closeWatchers()
  if (!child) process.exit(0)
  killChild(signal)
  setTimeout(() => process.exit(0), 5000)
}

function killChild(signal: NodeJS.Signals): void {
  if (!child || !child.pid) return
  try {
    if (process.platform === 'win32') child.kill(signal)
    else process.kill(-child.pid, signal)
  } catch {
    try { child.kill(signal) } catch { /* noop */ }
  }
}

function closeWatchers(): void {
  for (const entry of watchers.values()) entry.watcher.close()
  watchers.clear()
}

function shouldSkip(name: string): boolean {
  return name === '__pycache__'
    || name === '.mypy_cache'
    || name === '.pytest_cache'
    || name === '.ruff_cache'
    || name.endsWith('.pyc')
    || name.endsWith('.pyo')
    || name.endsWith('.swp')
    || name.endsWith('~')
}

function isRelevantChange(file: string): boolean {
  const ext = path.extname(file)
  return ext === '.py'
    || ext === '.toml'
    || ext === '.yaml'
    || ext === '.yml'
    || ext === '.json'
    || ext === '.md'
    || ext === ''
}

function relative(file: string): string {
  return path.relative(repoRoot, file) || '.'
}

function isNewer(source: string, target: string): boolean {
  try {
    return fs.statSync(source).mtimeMs > fs.statSync(target).mtimeMs
  } catch {
    return true
  }
}
