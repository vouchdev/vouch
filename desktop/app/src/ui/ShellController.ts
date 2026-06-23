import type { ShellView } from "../core/types";

export class ShellController {
  private readonly root: HTMLElement;
  private readonly loadingMessage: HTMLElement;
  private readonly errorMessage: HTMLElement;
  private readonly errorPath: HTMLElement;
  private readonly recentList: HTMLUListElement;

  constructor() {
    const root = document.getElementById("app");
    if (!root) throw new Error("missing #app root");
    this.root = root;
    this.loadingMessage = document.getElementById("loading-message")!;
    this.errorMessage = document.getElementById("error-message")!;
    this.errorPath = document.getElementById("error-path")!;
    this.recentList = document.getElementById("recent-list") as HTMLUListElement;
  }

  setView(view: ShellView): void {
    this.root.dataset.view = view;
    for (const section of ["loading", "error", "welcome"] as const) {
      const el = document.getElementById(section);
      if (!el) continue;
      el.classList.toggle("hidden", section !== view);
    }
  }

  setLoadingMessage(message: string): void {
    this.loadingMessage.textContent = message;
  }

  showError(selected: string, message: string): void {
    this.errorMessage.textContent = message;
    this.errorPath.textContent = selected;
    this.setView("error");
  }

  renderRecent(
    entries: { path: string; label: string }[],
    onSelect: (path: string) => void,
  ): void {
    this.recentList.replaceChildren();
    if (entries.length === 0) {
      const li = document.createElement("li");
      li.className = "recent-empty";
      li.textContent = "no recent knowledge bases";
      this.recentList.append(li);
      return;
    }
    for (const entry of entries) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "recent-item";
      btn.title = entry.path;
      btn.textContent = entry.label;
      btn.addEventListener("click", () => onSelect(entry.path));
      li.append(btn);
      this.recentList.append(li);
    }
  }

  bindActions(handlers: {
    onOpenKb: () => void;
    onNewKb: () => void;
    onCreateKb: () => void;
    onPickFolder: () => void;
  }): void {
    document.getElementById("btn-open-kb")?.addEventListener("click", handlers.onOpenKb);
    document.getElementById("btn-new-kb")?.addEventListener("click", handlers.onNewKb);
    document.getElementById("btn-create-kb")?.addEventListener("click", handlers.onCreateKb);
    document.getElementById("btn-pick-folder")?.addEventListener("click", handlers.onPickFolder);
  }
}
