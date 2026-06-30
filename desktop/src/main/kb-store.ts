import fs from "node:fs";
import path from "node:path";
import type { Prefs, RecentRoot } from "../shared/ipc";

interface StoreData {
  recentRoots: RecentRoot[];
  vouchPath: string | null;
  prefs: Prefs;
  window: { width: number; height: number };
}

const DEFAULTS: StoreData = {
  recentRoots: [],
  vouchPath: null,
  prefs: {
    autoOpenLast: true,
    closeToTray: true,
    notifyDualSolveReady: true,
    notifyNewPending: true,
    notifyProcessDown: true,
  },
  window: { width: 1280, height: 860 },
};

class KbStore {
  private file: string;
  private data: StoreData;

  constructor(userDataDir: string) {
    this.file = path.join(userDataDir, "state.json");
    this.data = this._load();
  }

  private _load(): StoreData {
    try {
      return { ...DEFAULTS, ...JSON.parse(fs.readFileSync(this.file, "utf8")) };
    } catch {
      return JSON.parse(JSON.stringify(DEFAULTS));
    }
  }

  save(): void {
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      fs.writeFileSync(this.file, JSON.stringify(this.data, null, 2));
    } catch {
      /* best-effort */
    }
  }

  get<K extends keyof StoreData>(key: K): StoreData[K] {
    return this.data[key];
  }

  set<K extends keyof StoreData>(key: K, value: StoreData[K]): void {
    this.data[key] = value;
    this.save();
  }

  get prefs(): Prefs {
    return this.data.prefs;
  }

  setPref(key: keyof Prefs, value: boolean): void {
    this.data.prefs[key] = value;
    this.save();
  }

  recordRoot(root: string): void {
    const now = Date.now();
    const list = (this.data.recentRoots || []).filter((r) => r.root !== root);
    list.unshift({ root, lastOpened: now });
    this.data.recentRoots = list.slice(0, 12);
    this.save();
  }

  get lastRoot(): string | null {
    const list = this.data.recentRoots || [];
    return list.length ? list[0].root : null;
  }
}

export { KbStore, DEFAULTS };
