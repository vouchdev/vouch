/** Desktop state schema helpers. */

import type { DesktopState } from "../core/types";
import { migrateState } from "../state/StateMigrator";

export function parseStateJson(text: string): DesktopState {
  try {
    return migrateState(JSON.parse(text) as unknown);
  } catch {
    return migrateState(null);
  }
}

export function serializeState(state: DesktopState): string {
  return JSON.stringify(state, null, 2) + "\n";
}
