#!/usr/bin/env tsx
/*
 * gen-methods.ts — turn the verified vouch surface catalog (src/catalog/methods.json,
 * extracted from the running `vouch` source) into the shared typed method catalog
 * (src/shared/methods.gen.ts).
 *
 * Enrichment is purely additive and deterministic:
 *   - control:     the input control the form generator should render for each param
 *   - enum:        option lists for select/combobox params
 *   - refKind:     for id-reference params, which artifact kind a typeahead searches
 *   - file:        'open' | 'save' | 'under-root' for native-picker params
 *   - longRunning: methods that need a longer JSONL timeout
 *
 * Also normalizes kb.trace's param keys (from_id/to_id -> from/to) to match the
 * JSONL wire keys the handler actually reads, and self-checks coverage.
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import type { Method } from '../src/shared/methods.types'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const SRC = path.join(ROOT, 'src/catalog/methods.json')
const OUT = path.join(ROOT, 'src/shared/methods.gen.ts')

// --- verified enum sets (from src/vouch/models.py + handlers) ---------------
const ENUMS: Record<string, string[]> = {
  backend: ['auto', 'embedding', 'fts5', 'substring', 'hybrid'],
  claim_type: ['fact', 'decision', 'preference', 'workflow', 'observation', 'question', 'warning'],
  // ClaimStatus — the list_claims status filter operates on claim status, not proposal status
  status: ['working', 'actionable', 'stable', 'contested', 'superseded', 'archived', 'redacted'],
  entity_type: ['person', 'project', 'repo', 'company', 'concept', 'decision', 'workflow',
    'file', 'api', 'incident', 'source', 'agent', 'tool', 'team', 'system'],
  relation: ['uses', 'depends_on', 'contradicts', 'supersedes', 'supports', 'caused_by',
    'owned_by', 'derived_from', 'similar_to', 'blocks', 'implements', 'references',
    'mentions', 'relates_to'],
  source_type: ['file', 'url', 'transcript', 'message', 'commit', 'issue', 'screenshot',
    'pdf', 'audio', 'video', 'folder'],
  page_type: ['entity', 'concept', 'decision', 'workflow', 'session', 'index', 'log',
    'report', 'source-summary'],
  format: ['dot', 'mermaid'],
  on_conflict: ['skip', 'overwrite', 'fail'],
  op: ['archive', 'contradict', 'supersede'],
}
// page_type is a combobox (a KB may declare extra kinds in config)
const COMBOBOX = new Set(['page_type'])

// --- id-reference params -> the artifact kind a typeahead should search ------
const REF: Record<string, string> = {
  evidence: 'source', entities: 'entity', claim_ids: 'claim', entity_ids: 'entity',
  source_ids: 'source', node_id: 'node', page_id: 'page', claim_id: 'claim',
  entity_id: 'entity', relation_id: 'relation', old_claim_id: 'claim',
  new_claim_id: 'claim', claim_a: 'claim', claim_b: 'claim', src: 'node',
  target: 'node', proposal_id: 'proposal', session_id: 'session', from: 'node',
  to: 'node', against: 'claim',
}

// --- native-picker params ----------------------------------------------------
const FILE: Record<string, string> = {
  path: 'under-root',   // register_source_from_path — must live under the KB root
  out_path: 'save',     // export
  bundle_path: 'open',  // export_check / import_check / import_apply
  queries_path: 'open', // eval_embeddings
}

// params rendered as multi-line text
const TEXTAREA = new Set(['text', 'body', 'description', 'note', 'rationale',
  'content', 'reason', 'task', 'query'])
// 0..1 sliders
const SLIDER = new Set(['confidence', 'min_score', 'threshold'])

// methods whose JSONL call may run long — the client uses an extended timeout
const LONG_RUNNING = new Set([
  'kb.index_rebuild', 'kb.reindex_embeddings', 'kb.export', 'kb.import_apply',
  'kb.provenance_rebuild', 'kb.dedup_scan', 'kb.eval_embeddings', 'kb.source_verify',
  'kb.synthesize',
])

// group -> the left-rail view it belongs to (some groups merge into one view)
const VIEW: Record<string, string> = {
  read: 'browse', search: 'search', list: 'browse', propose: 'propose',
  lifecycle: 'review', session: 'sessions', graph: 'graph',
  maintenance: 'maintenance', 'export-import': 'export-import', audit: 'audit',
}
// methods promoted to the Dashboard / Search views regardless of raw group
const VIEW_OVERRIDE: Record<string, string> = {
  'kb.capabilities': 'dashboard', 'kb.status': 'dashboard', 'kb.stats': 'dashboard',
  'kb.search': 'search', 'kb.context': 'search', 'kb.synthesize': 'search',
  'kb.list_pending': 'review', 'kb.source_verify': 'maintenance',
  'kb.cite': 'browse',
}

function controlFor(_methodName: string, p: any): string {
  if (FILE[p.name]) return 'file'
  if (ENUMS[p.name]) return COMBOBOX.has(p.name) ? 'combobox' : 'select'
  if (p.type === 'boolean') return 'toggle'
  if (p.type === 'integer') return 'integer'
  if (p.type === 'number') return SLIDER.has(p.name) ? 'slider' : 'number'
  if (p.type === 'object') return 'json'
  if (/array/i.test(p.type)) return 'tags'
  if (TEXTAREA.has(p.name)) return 'textarea'
  return 'text'
}

export function enrich(methods: any[]): Method[] {
  if (!Array.isArray(methods) || methods.length === 0) {
    throw new Error('catalog empty')
  }

  const missingEnum: string[] = []
  for (const m of methods) {
    // normalize kb.trace param keys to the JSONL wire keys the handler reads
    if (m.name === 'kb.trace') {
      for (const p of m.params || []) {
        if (p.name === 'from_id') p.name = 'from'
        if (p.name === 'to_id') p.name = 'to'
      }
    }
    m.view = VIEW_OVERRIDE[m.name] || VIEW[m.group] || 'browse'
    m.longRunning = LONG_RUNNING.has(m.name)
    for (const p of m.params || []) {
      p.control = controlFor(m.name, p)
      if (ENUMS[p.name]) p.enum = ENUMS[p.name]
      if (COMBOBOX.has(p.name)) p.combobox = true
      if (REF[p.name]) {
        p.refKind = REF[p.name]
        if (/array/i.test(p.type)) p.refMulti = true
      }
      if (FILE[p.name]) p.file = FILE[p.name]
      // sanity: any param we marked select/combobox must have options
      if ((p.control === 'select' || p.control === 'combobox') && !p.enum) {
        missingEnum.push(`${m.name}.${p.name}`)
      }
    }
  }

  if (missingEnum.length) {
    throw new Error('select/combobox params without enum options: ' + missingEnum.join(', '))
  }

  return methods as Method[]
}

function main() {
  const methods = enrich(JSON.parse(fs.readFileSync(SRC, 'utf8')))

  const banner = '// GENERATED by scripts/gen-methods.ts — do not edit by hand.\n'
  const body =
    `import type { Method } from './methods.types'\n\n` +
    `export const methods: Method[] = ${JSON.stringify(methods, null, 2)}\n\n` +
    `export type MethodName = ${methods.map(m => JSON.stringify(m.name)).join(' | ')}\n\n` +
    `export default methods\n`
  fs.writeFileSync(OUT, banner + body)

  // --- coverage report ---
  const byView: Record<string, string[]> = {}
  for (const m of methods) (byView[m.view] ||= []).push(m.name)
  const enumParams: string[] = []
  for (const m of methods) for (const p of (m.params || [])) if ((p as any).enum) enumParams.push(`${m.name}.${p.name}`)

  console.log(`wrote ${methods.length} methods -> ${path.relative(ROOT, OUT)}`)
  console.log('views:')
  for (const v of Object.keys(byView).sort()) console.log(`  ${v.padEnd(14)} ${byView[v].length}`)
  console.log(`enum/combobox params wired: ${enumParams.length}`)
  console.log(`long-running methods: ${methods.filter(m => (m as any).longRunning).length}`)
  if (methods.length !== 54) console.warn(`WARNING: expected 54 methods, got ${methods.length}`)
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main()
