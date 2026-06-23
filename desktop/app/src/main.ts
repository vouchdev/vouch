import { createLogger } from "./core/logger";
import {
  checkKbFolder,
  initKbAt,
  loadDesktopState,
  newKbDialog,
  openKbDialog,
  openRecentKb,
  switchKb,
} from "./ipc/commands";
import {
  onKbError,
  onKbSwitched,
  onMenuNewKb,
  onMenuOpenKb,
  onMenuRecentKb,
} from "./ipc/events";
import type { KbSwitchedPayload, KbErrorPayload } from "./ipc/events";
import { ShellController } from "./ui/ShellController";

const log = createLogger("main");

let pendingErrorPath: string | null = null;

async function trySwitch(projectRoot: string): Promise<void> {
  const shell = getShell();
  shell.setView("loading");
  shell.setLoadingMessage(`opening ${projectRoot}…`);
  const result = await switchKb(projectRoot);
  if (!result.ok) {
    shell.showError(projectRoot, result.error ?? "failed to switch knowledge base");
    pendingErrorPath = projectRoot;
    return;
  }
  log.info("switched KB", result);
}

async function handleOpenDialog(): Promise<void> {
  const selected = await openKbDialog();
  if (!selected) return;
  const check = await checkKbFolder(selected);
  const shell = getShell();
  if (!check.ok) {
    shell.showError(selected, check.message);
    pendingErrorPath = selected;
    return;
  }
  if (!check.project_root) return;
  await trySwitch(check.project_root);
}

async function handleNewDialog(): Promise<void> {
  const selected = await newKbDialog();
  if (!selected) return;
  const shell = getShell();
  shell.setView("loading");
  shell.setLoadingMessage("initialising knowledge base…");
  try {
    const init = await initKbAt(selected);
    if (!init.ok || !init.starter_present) {
      shell.showError(selected, "init did not produce a starter claim");
      pendingErrorPath = selected;
      return;
    }
    await trySwitch(init.project_root);
  } catch (err) {
    shell.showError(selected, String(err));
    pendingErrorPath = selected;
  }
}

async function handleCreateKbHere(): Promise<void> {
  if (!pendingErrorPath) return;
  const shell = getShell();
  shell.setView("loading");
  shell.setLoadingMessage("creating knowledge base…");
  const init = await initKbAt(pendingErrorPath);
  if (!init.ok) {
    shell.showError(pendingErrorPath, "failed to create knowledge base");
    return;
  }
  await trySwitch(init.project_root);
}

async function bootstrap(): Promise<void> {
  const shell = getShell();
  shell.setView("loading");
  shell.setLoadingMessage("loading desktop state…");

  const state = await loadDesktopState();
  shell.renderRecent(state.recent_kbs, (path: string) => {
    void openRecentKb(path).then((result) => {
      if (!result.ok) shell.showError(path, result.error ?? "failed to open recent KB");
    });
  });

  if (state.last_kb) {
    shell.setLoadingMessage("restoring last knowledge base…");
    const result = await switchKb(state.last_kb);
    if (result.ok) return;
    log.warn("last KB failed", result);
  }

  shell.setView("welcome");
}

let shellSingleton: ShellController | null = null;

function getShell(): ShellController {
  if (!shellSingleton) shellSingleton = new ShellController();
  return shellSingleton;
}

function wireUi(): void {
  const shell = getShell();
  shell.bindActions({
    onOpenKb: () => void handleOpenDialog(),
    onNewKb: () => void handleNewDialog(),
    onCreateKb: () => void handleCreateKbHere(),
    onPickFolder: () => void handleOpenDialog(),
  });
}

function wireEvents(): void {
  void onKbSwitched((payload: KbSwitchedPayload) => {
    log.info("kb switched event", payload);
    document.title = `vouch · ${payload.kb_label}`;
  });
  void onKbError((payload: KbErrorPayload) => {
    getShell().showError(payload.selected, payload.message);
    pendingErrorPath = payload.selected;
  });
  void onMenuOpenKb(() => void handleOpenDialog());
  void onMenuNewKb(() => void handleNewDialog());
  void onMenuRecentKb((path: string) => void trySwitch(path));
}

wireUi();
wireEvents();
void bootstrap();
