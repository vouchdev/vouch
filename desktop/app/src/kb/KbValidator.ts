/** Client-side KB folder validation (mirrors Rust/Python checks). */

import type { KbCheckResult } from "../core/types";

const KB_DIRNAME = ".vouch";

export function labelForPath(path: string): string {
  const normalized = path.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export function resolveSelection(selected: string): string {
  const normalized = selected.replace(/\\/g, "/").replace(/\/+$/, "");
  if (normalized.endsWith(`/${KB_DIRNAME}`) || normalized.endsWith(KB_DIRNAME)) {
    const idx = normalized.lastIndexOf(`/${KB_DIRNAME}`);
    if (idx >= 0) return normalized.slice(0, idx) || normalized;
  }
  return normalized;
}

export function formatKbCheckMessage(result: KbCheckResult): string {
  if (result.ok) return "Knowledge base is ready.";
  if (result.message.includes(KB_DIRNAME)) {
    return `This folder does not contain a ${KB_DIRNAME}/ directory. You can create one here.`;
  }
  return result.message;
}

export function isLikelyProjectRoot(path: string): boolean {
  return path.length > 0 && !path.includes("\0");
}
