import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export interface KbSwitchedPayload {
  project_root: string;
  kb_label: string;
  base_url: string;
}

export interface KbErrorPayload {
  selected: string;
  message: string;
}

export async function onKbSwitched(handler: (payload: KbSwitchedPayload) => void): Promise<UnlistenFn> {
  return listen<KbSwitchedPayload>("kb-switched", (event) => handler(event.payload));
}

export async function onKbError(handler: (payload: KbErrorPayload) => void): Promise<UnlistenFn> {
  return listen<KbErrorPayload>("kb-error", (event) => handler(event.payload));
}

export async function onMenuOpenKb(handler: () => void): Promise<UnlistenFn> {
  return listen("menu-open-kb", () => handler());
}

export async function onMenuNewKb(handler: () => void): Promise<UnlistenFn> {
  return listen("menu-new-kb", () => handler());
}

export async function onMenuRecentKb(handler: (path: string) => void): Promise<UnlistenFn> {
  return listen<string>("menu-recent-kb", (event) => handler(event.payload));
}
