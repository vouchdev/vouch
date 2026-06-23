import type { DesktopState, RecentKbEntry } from "../core/types";
import { MAX_RECENT_KBS, STATE_VERSION } from "../core/constants";

export function emptyState(): DesktopState {
  return {
    version: STATE_VERSION,
    last_kb: null,
    recent_kbs: [],
  };
}

export function normalizePath(path: string): string {
  return path.replace(/\\/g, "/").replace(/\/+$/, "");
}

export function dedupeRecent(entries: RecentKbEntry[]): RecentKbEntry[] {
  const seen = new Set<string>();
  const out: RecentKbEntry[] = [];
  for (const entry of entries) {
    const key = normalizePath(entry.path).toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(entry);
  }
  return out.slice(0, MAX_RECENT_KBS);
}

export function labelForPath(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export function touchRecent(
  state: DesktopState,
  projectRoot: string,
  label?: string,
): DesktopState {
  const now = new Date().toISOString();
  const entry: RecentKbEntry = {
    path: projectRoot,
    label: label ?? labelForPath(projectRoot),
    opened_at: now,
  };
  const filtered = state.recent_kbs.filter(
    (e) => normalizePath(e.path).toLowerCase() !== normalizePath(projectRoot).toLowerCase(),
  );
  return {
    version: STATE_VERSION,
    last_kb: projectRoot,
    recent_kbs: dedupeRecent([entry, ...filtered]),
  };
}
