import { invoke } from "@tauri-apps/api/core";
import type { DesktopState, KbCheckResult, KbInitResult, SidecarStatus, SwitchKbResult } from "../core/types";

export async function loadDesktopState(): Promise<DesktopState> {
  return invoke<DesktopState>("load_state");
}

export async function touchRecentKb(projectRoot: string, label?: string): Promise<DesktopState> {
  return invoke<DesktopState>("touch_recent_kb", { projectRoot, label: label ?? null });
}

export async function checkKbFolder(selected: string): Promise<KbCheckResult> {
  return invoke<KbCheckResult>("check_kb_folder", { selected });
}

export async function initKbAt(selected: string): Promise<KbInitResult> {
  return invoke<KbInitResult>("init_kb_at", { selected });
}

export async function switchKb(projectRoot: string): Promise<SwitchKbResult> {
  return invoke<SwitchKbResult>("switch_kb", { projectRoot });
}

export async function openKbDialog(): Promise<string | null> {
  return invoke<string | null>("open_kb_dialog");
}

export async function newKbDialog(): Promise<string | null> {
  return invoke<string | null>("new_kb_dialog");
}

export async function openRecentKb(path: string): Promise<SwitchKbResult> {
  return invoke<SwitchKbResult>("open_recent_kb", { path });
}

export async function getSidecarStatus(): Promise<SidecarStatus> {
  return invoke<SidecarStatus>("sidecar_status");
}

export async function navigateToReview(): Promise<void> {
  return invoke("navigate_to_review");
}

export async function showWelcome(): Promise<void> {
  return invoke("show_welcome");
}
