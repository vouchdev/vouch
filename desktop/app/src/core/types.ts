/** Core type definitions for the desktop shell. */

export interface RecentKbEntry {
  path: string;
  label: string;
  opened_at: string;
}

export interface DesktopState {
  version: number;
  last_kb: string | null;
  recent_kbs: RecentKbEntry[];
}

export interface KbCheckResult {
  ok: boolean;
  project_root: string | null;
  kb_dir: string | null;
  message: string;
}

export interface KbInitResult {
  ok: boolean;
  project_root: string;
  kb_dir: string;
  claim_id: string;
  starter_present: boolean;
  label: string;
}

export interface HealthzResponse {
  ok: boolean;
  kb: string;
  kb_label?: string;
  pending: number;
  auth: boolean;
  clients: number;
}

export interface SidecarStatus {
  running: boolean;
  base_url: string | null;
  project_root: string | null;
  kb_label: string | null;
  pid: number | null;
}

export type ShellView = "loading" | "welcome" | "error" | "review";

export interface SwitchKbResult {
  ok: boolean;
  base_url?: string;
  kb_label?: string;
  error?: string;
}
