/**
 * OpenClaw plugin entry for the vouch-context engine (#228).
 *
 * The host loads this module via openclaw.plugin.json → openclaw.extensions.
 * Runtime assembly delegates to the Python engine through `vouch openclaw-rpc`
 * so the cited synthesis path stays identical to unit tests and kb.context.
 *
 * Enable in openclaw.json:
 *   plugins.slots.contextEngine: "vouch-context"
 */

import { spawnSync } from 'node:child_process';

export const ENGINE_ID = 'vouch-context';
export const ENGINE_NAME = 'Vouch Context Engine';

/** @typedef {import('node:child_process').SpawnSyncReturns<string>} SpawnResult */

/**
 * @param {string} method
 * @param {Record<string, unknown>} params
 * @returns {Record<string, unknown>}
 */
function callPythonEngine(method, params) {
  const envelope = JSON.stringify({
    id: 'openclaw',
    method,
    params,
  });
  /** @type {SpawnResult} */
  const proc = spawnSync('vouch', ['openclaw-rpc'], {
    input: envelope,
    encoding: 'utf8',
    env: process.env,
    maxBuffer: 16 * 1024 * 1024,
  });
  if (proc.error) {
    throw proc.error;
  }
  if (proc.status !== 0) {
    const detail = (proc.stderr || proc.stdout || '').trim();
    throw new Error(
      `vouch openclaw-rpc exited ${proc.status}${detail ? `: ${detail}` : ''}`,
    );
  }
  let parsed;
  try {
    parsed = JSON.parse(String(proc.stdout || '{}'));
  } catch (err) {
    throw new Error(`vouch openclaw-rpc returned invalid json: ${err}`);
  }
  if (!parsed.ok) {
    const msg = parsed.error?.message || 'engine rpc failed';
    throw new Error(msg);
  }
  return parsed.result;
}

/** @type {{ id: string; name: string; description: string; register: (api: any) => void }} */
const entry = {
  id: 'vouch-context-engine',
  name: 'Vouch Context Engine',
  description:
    'Review-gated KB context: cited retrieval + salience reflex + hot memory on every assemble()',

  register(api) {
    api.registerContextEngine(ENGINE_ID, (ctx) => {
      const workspaceDir = ctx.workspaceDir;
      const kbPath = ctx.kbPath ?? ctx.kb_path;
      const agent = ctx.agent;
      const project = ctx.project;

      const baseParams = () => ({
        workspaceDir,
        kbPath,
        agent,
        project,
      });

      return {
        info: {
          id: ENGINE_ID,
          name: ENGINE_NAME,
          version: '0.1.0',
          ownsCompaction: false,
        },

        async ingest({ sessionId, message, isHeartbeat }) {
          return callPythonEngine('ingest', {
            ...baseParams(),
            sessionId,
            message,
            isHeartbeat,
          });
        },

        async assemble(params) {
          return callPythonEngine('assemble', {
            ...baseParams(),
            ...params,
          });
        },

        async compact(params) {
          return callPythonEngine('compact', {
            ...baseParams(),
            ...params,
          });
        },
      };
    });
  },
};

export default entry;
