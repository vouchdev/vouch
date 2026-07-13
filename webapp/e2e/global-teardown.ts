import { readFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'

export default async function globalTeardown(): Promise<void> {
  const stateFile = join(process.cwd(), 'e2e', '.kb-state.json')
  try {
    const { dir, pid } = JSON.parse(readFileSync(stateFile, 'utf-8')) as { dir: string; pid: number }
    if (pid) process.kill(-pid, 'SIGTERM') // negative pid: kill the detached group
    rmSync(dir, { recursive: true, force: true })
    rmSync(stateFile, { force: true })
  } catch {
    /* already gone */
  }
}
