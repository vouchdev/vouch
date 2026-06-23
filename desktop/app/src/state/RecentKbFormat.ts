import { labelForPath } from "../kb/KbValidator";
import type { RecentKbEntry } from "../core/types";

export function formatRecentMenuLabel(entry: RecentKbEntry): string {
  if (entry.label.trim().length > 0) return entry.label;
  return labelForPath(entry.path);
}

export function sortRecentByOpenedAt(entries: RecentKbEntry[]): RecentKbEntry[] {
  return [...entries].sort((a, b) => b.opened_at.localeCompare(a.opened_at));
}

export function truncateRecent(entries: RecentKbEntry[], max: number): RecentKbEntry[] {
  return entries.slice(0, max);
}
