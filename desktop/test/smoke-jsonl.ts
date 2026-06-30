/*
 * smoke-jsonl.ts — exercises the JSONL client against the REAL vouch binary.
 * Run: npm run smoke [kbRoot]
 * No Electron needed (pure child_process). Exits non-zero on any failure.
 */
import assert from 'node:assert'
import { JsonlClient } from '../src/main/jsonl-client.js'
import { resolveVouch, hasKb } from '../src/main/vouch-locator.js'

const root = process.argv[2] ?? process.env.VOUCH_TEST_KB

async function main(): Promise<void> {
  assert(root, 'usage: npm run smoke <kbRoot>')
  const launcher = resolveVouch()
  assert(launcher, 'could not resolve a runnable vouch')
  console.log(
    `vouch: ${launcher.cmd} ${launcher.baseArgs.join(' ')} (${launcher.kind}) ${launcher.version}`,
  )
  assert(hasKb(root), `no .vouch in ${root}`)

  const client = new JsonlClient(launcher, {
    root,
    longMethods: new Set(['kb.synthesize', 'kb.index_rebuild']),
  })
  const stderr: string[] = []
  client.on('stderr', (l: string) => stderr.push(l))
  client.start()

  let failures = 0
  const check = async (name: string, fn: () => Promise<void>): Promise<void> => {
    try {
      await fn()
      console.log(`  ok  ${name}`)
    } catch (e: unknown) {
      failures++
      console.log(`  FAIL ${name}: ${(e as Error).message}`)
    }
  }

  await check('kb.capabilities advertises a method list', async () => {
    const caps = (await client.call('kb.capabilities', {})) as {
      name: string
      methods: string[]
    }
    assert.equal(caps.name, 'vouch')
    // the installed binary advertises the subset it actually handles (43 in
    // 0.1.0; the dev source tree has 54). The UI gates against this list.
    assert(
      Array.isArray(caps.methods) && caps.methods.length >= 40,
      `methods=${caps.methods && caps.methods.length}`,
    )
  })

  await check('kb.status returns counts', async () => {
    const s = await client.call('kb.status', {})
    assert(s && typeof s === 'object')
  })

  await check('kb.search finds the starter claim', async () => {
    const r = (await client.call('kb.search', { query: 'agent', limit: 5 })) as {
      hits?: unknown[]
    }
    assert(Array.isArray(r.hits) || Array.isArray(r), 'no hits array')
  })

  await check('kb.list_claims works', async () => {
    const r = await client.call('kb.list_claims', {})
    assert(Array.isArray(r))
  })

  await check('kb.list_pending works', async () => {
    const r = await client.call('kb.list_pending', {})
    assert(Array.isArray(r))
  })

  await check('unknown method -> method_not_found error', async () => {
    try {
      await client.call('kb.nope', {})
      assert.fail('should have thrown')
    } catch (e: unknown) {
      assert.equal((e as { code?: string }).code, 'method_not_found', `code=${(e as { code?: string }).code}`)
    }
  })

  await check('missing required param -> error', async () => {
    try {
      await client.call('kb.read_claim', {})
      assert.fail('should have thrown')
    } catch (e: unknown) {
      const code = (e as { code?: string }).code
      assert(
        ['missing_param', 'invalid_request', 'internal_error'].includes(code ?? ''),
        `code=${code}`,
      )
    }
  })

  await check('propose_claim enforces the citation gate (uncited claim rejected)', async () => {
    try {
      await client.call('kb.propose_claim', {
        text: 'smoke test claim',
        evidence: [],
        dry_run: true,
      })
      assert.fail('uncited claim should be rejected')
    } catch (e: unknown) {
      const err = e as { code?: string; message?: string }
      assert.equal(err.code, 'invalid_request', `code=${err.code}`)
      assert(/cite|evidence|source/i.test(err.message ?? ''), `unexpected message: ${err.message}`)
    }
  })

  await client.stop()
  if (stderr.length) console.log(`  (captured ${stderr.length} stderr lines)`)
  if (failures) {
    console.error(`\n${failures} check(s) failed`)
    process.exit(1)
  }
  console.log('\nall smoke checks passed')
}

main().catch((e: unknown) => {
  console.error(e)
  process.exit(1)
})
