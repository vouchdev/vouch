import { describe, it, expect } from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { HttpClient } from '../src/main/http-client'

function writeFakeVouch(dir: string): string {
  const script = path.join(dir, 'fake-vouch.js')
  fs.writeFileSync(script, `
const fs = require("node:fs");
const http = require("node:http");
const out = process.argv[2];
const args = process.argv.slice(3);
fs.writeFileSync(out, JSON.stringify(args));
const bindIdx = args.indexOf("--bind");
const port = Number((args[bindIdx + 1] || "").split(":").pop());
const server = http.createServer((req, res) => {
  if (req.url === "/healthz") {
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ ok: true }));
  } else {
    res.statusCode = 404;
    res.end("not found");
  }
});
server.listen(port, "127.0.0.1");
process.on("SIGINT", () => server.close(() => process.exit(0)));
process.on("SIGTERM", () => server.close(() => process.exit(0)));
`)
  return script
}

describe('HttpClient', () => {
  it('launches review-ui with the dual-solve sandbox flag', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vouch-desktop-http-'))
    const argsFile = path.join(dir, 'args.json')
    const script = writeFakeVouch(dir)
    const client = new HttpClient(
      { cmd: process.execPath, baseArgs: [script, argsFile] },
      { root: dir, env: {} },
    )
    try {
      const ready = await client.ensure({ allowDualSolve: true })
      expect(ready.up).toBe(true)
      const args = JSON.parse(fs.readFileSync(argsFile, 'utf8')) as string[]
      expect(args).toContain('review-ui')
      expect(args).toContain('--allow-dual-solve')
      expect(args).toContain('--dual-solve-sandbox')
      expect(args).not.toContain('--dual-solve-sandbox-image')
    } finally {
      await client.stop()
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })

  it('forwards a custom sandbox image when configured', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vouch-desktop-http-'))
    const argsFile = path.join(dir, 'args.json')
    const script = writeFakeVouch(dir)
    const client = new HttpClient(
      { cmd: process.execPath, baseArgs: [script, argsFile] },
      { root: dir, env: {}, sandboxImage: 'example/coder:dev' },
    )
    try {
      await client.ensure({ allowDualSolve: true })
      const args = JSON.parse(fs.readFileSync(argsFile, 'utf8')) as string[]
      const idx = args.indexOf('--dual-solve-sandbox-image')
      expect(idx).toBeGreaterThanOrEqual(0)
      expect(args[idx + 1]).toBe('example/coder:dev')
    } finally {
      await client.stop()
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })
})
