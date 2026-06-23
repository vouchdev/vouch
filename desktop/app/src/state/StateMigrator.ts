import type { DesktopState } from "../core/types";
import { STATE_VERSION } from "../core/constants";
import { emptyState } from "./RecentKbList";

export function migrateState(raw: unknown): DesktopState {
  if (!raw || typeof raw !== "object") {
    return emptyState();
  }
  const obj = raw as Record<string, unknown>;
  const version = typeof obj.version === "number" ? obj.version : 1;
  if (version > STATE_VERSION) {
    return emptyState();
  }
  const recent = Array.isArray(obj.recent_kbs) ? obj.recent_kbs : [];
  const parsedRecent = recent
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object")
    .map((item) => ({
      path: String(item.path ?? ""),
      label: String(item.label ?? ""),
      opened_at: String(item.opened_at ?? ""),
    }))
    .filter((item) => item.path.length > 0);

  return {
    version: STATE_VERSION,
    last_kb: typeof obj.last_kb === "string" ? obj.last_kb : null,
    recent_kbs: parsedRecent,
  };
}
