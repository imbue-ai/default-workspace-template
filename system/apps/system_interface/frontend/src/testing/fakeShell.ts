/**
 * A fake shell for the store and page tests: the desktop routes over in-memory desktops and
 * layouts (with the shell's stamp rule, so a stale save is refused), and a socket whose events a
 * test delivers by hand. Every call is recorded so a test can assert what reached the shell.
 */

import type { DesktopApi } from "../store/DesktopStore";
import type { PlacementsSaveRequest, WindowOpenOutcome, WindowOpenRequest } from "../model/api";
import { StalePlacementsSaveError } from "../model/api";
import type {
  AvatarCatalog,
  ClientRecord,
  Desktop,
  DesktopShortcut,
  EntryPresentation,
  GridCell,
  Layout,
  SharingMode,
  StoredWindowPath,
  Wallpaper,
  WindowRecord,
} from "../model/records";
import { withWindowPlacedOnOpen, withWindowRaised, withoutPlacement } from "../geometry/stack";
import type { DesktopSocket, SocketHandlers } from "../store/socket";
import { clientRecord } from "./records";

export class FakeDesktopApi implements DesktopApi {
  desktops: Desktop[] = [];
  clients: ClientRecord[] = [];
  /** ``<desktop>/<client>`` -> the stored placements and their stamp. */
  readonly layouts = new Map<string, Pick<Layout, "updated_at" | "placements">>();
  /** ``<client>/<window>`` -> the client's own path and title for an independent window. */
  readonly windowPaths = new Map<string, StoredWindowPath>();
  readonly calls: string[] = [];
  /** A refusal every route raises while set. */
  refusal: string | null = null;
  avatars: AvatarCatalog = {
    designs: [
      { id: "gummy-seal", label: "Gummy seal", source_path: null },
      { id: "jelly-cat", label: "Jelly cat", source_path: null },
    ],
    selected: "gummy-seal",
    default: "gummy-seal",
  };
  private stampCounter = 0;
  private windowCounter = 0;

  private refuse(): void {
    if (this.refusal !== null) throw new Error(this.refusal);
  }

  private stamp(): string {
    this.stampCounter += 1;
    return `2026-09-19T00:00:${String(this.stampCounter).padStart(2, "0")}Z`;
  }

  private desktop(desktopId: string): Desktop {
    const desktop = this.desktops.find((candidate) => candidate.id === desktopId);
    if (desktop === undefined) throw new Error(`No desktop ${desktopId}`);
    return desktop;
  }

  private replace(desktop: Desktop): Desktop {
    this.desktops = this.desktops.map((candidate) => (candidate.id === desktop.id ? desktop : candidate));
    return desktop;
  }

  /** The client's stored layout of the desktop, with its paths for the desktop's independent windows. */
  layoutOf(desktopId: string, clientId: string): Layout {
    const stored = this.layouts.get(`${desktopId}/${clientId}`) ?? { updated_at: null, placements: [] };
    const window_paths: Record<string, StoredWindowPath> = {};
    for (const window of this.desktops.find((desktop) => desktop.id === desktopId)?.windows ?? []) {
      const path = this.windowPaths.get(`${clientId}/${window.id}`);
      if (window.scope === "independent" && path !== undefined) window_paths[window.id] = path;
    }
    return { ...stored, window_paths };
  }

  writeLayout(desktopId: string, clientId: string, layout: Pick<Layout, "updated_at" | "placements">): Layout {
    const stamped = { ...layout, updated_at: this.stamp() };
    this.layouts.set(`${desktopId}/${clientId}`, stamped);
    return this.layoutOf(desktopId, clientId);
  }

  async fetchDesktops(): Promise<Desktop[]> {
    this.calls.push("fetchDesktops");
    this.refuse();
    return [...this.desktops];
  }

  async createDesktop(name: string, color: string, glyph: number): Promise<Desktop> {
    this.calls.push(`createDesktop:${name}`);
    this.refuse();
    const created: Desktop = {
      id: name.toLowerCase().replace(/[^a-z0-9]+/g, "-"),
      name,
      color,
      glyph,
      sharing: "shared",
      wallpaper: null,
      shortcuts: [],
      windows: [],
    };
    this.desktops = [...this.desktops, created];
    return created;
  }

  async updateDesktopSettings(
    desktopId: string,
    name: string,
    color: string,
    glyph: number,
    sharing: SharingMode,
  ): Promise<Desktop> {
    this.calls.push(`updateDesktopSettings:${desktopId}:${name}`);
    this.refuse();
    return this.replace({ ...this.desktop(desktopId), name, color, glyph, sharing });
  }

  async setDesktopWallpaper(desktopId: string, wallpaper: Wallpaper | null): Promise<Desktop> {
    this.calls.push(`setDesktopWallpaper:${desktopId}:${wallpaper?.name ?? "null"}`);
    this.refuse();
    return this.replace({ ...this.desktop(desktopId), wallpaper });
  }

  async deleteDesktop(desktopId: string): Promise<string> {
    this.calls.push(`deleteDesktop:${desktopId}`);
    this.refuse();
    this.desktop(desktopId);
    // The shell's own refusal: the last desktop stays.
    if (this.desktops.length === 1) throw new Error("the last desktop cannot be deleted");
    this.desktops = this.desktops.filter((candidate) => candidate.id !== desktopId);
    return this.desktops[0].id;
  }

  async setDesktopShortcut(desktopId: string, shortcut: DesktopShortcut): Promise<Desktop> {
    this.calls.push(
      `setDesktopShortcut:${desktopId}:${shortcut.target.app}:${shortcut.target.launch}:${shortcut.cell.column},${shortcut.cell.row}`,
    );
    this.refuse();
    const desktop = this.desktop(desktopId);
    const others = desktop.shortcuts.filter(
      (candidate) =>
        candidate.target.app !== shortcut.target.app || candidate.target.launch !== shortcut.target.launch,
    );
    return this.replace({ ...desktop, shortcuts: [...others, shortcut] });
  }

  async moveDesktopShortcut(desktopId: string, app: string, launch: string, cell: GridCell): Promise<Desktop> {
    this.calls.push(`moveDesktopShortcut:${desktopId}:${app}:${launch}:${cell.column},${cell.row}`);
    this.refuse();
    const desktop = this.desktop(desktopId);
    return this.replace({
      ...desktop,
      shortcuts: desktop.shortcuts.map((candidate) =>
        candidate.target.app === app && candidate.target.launch === launch ? { ...candidate, cell } : candidate,
      ),
    });
  }

  async removeDesktopShortcut(desktopId: string, app: string, launch: string): Promise<Desktop> {
    this.calls.push(`removeDesktopShortcut:${desktopId}:${app}:${launch}`);
    this.refuse();
    const desktop = this.desktop(desktopId);
    return this.replace({
      ...desktop,
      shortcuts: desktop.shortcuts.filter(
        (candidate) => candidate.target.app !== app || candidate.target.launch !== launch,
      ),
    });
  }

  async openWindow(desktopId: string, request: WindowOpenRequest): Promise<WindowOpenOutcome> {
    this.calls.push(
      `openWindow:${desktopId}:${request.app}:${request.path}:${request.ifPresent}:${request.launch ?? "-"}`,
    );
    this.refuse();
    const desktop = this.desktop(desktopId);
    const existing = desktop.windows.find(
      (candidate) => candidate.app === request.app && candidate.path === request.path,
    );
    if (request.ifPresent === "focus" && existing !== undefined) {
      this.writeLayout(
        desktopId,
        request.clientId,
        withWindowRaised(this.layoutOf(desktopId, request.clientId), existing.id),
      );
      return { window: existing, isNew: false };
    }
    this.windowCounter += 1;
    const window: WindowRecord = {
      id: `win-${String(this.windowCounter).padStart(16, "0")}`,
      app: request.app,
      path: request.path,
      title: "",
      opened_at: this.stamp(),
      is_settling: request.launch !== null,
      is_pinned: false,
      scope: "linked",
    };
    this.replace({ ...desktop, windows: [...desktop.windows, window] });
    this.writeLayout(
      desktopId,
      request.clientId,
      withWindowPlacedOnOpen(this.layoutOf(desktopId, request.clientId), window.id),
    );
    return { window, isNew: true };
  }

  async closeWindow(desktopId: string, windowId: string): Promise<void> {
    this.calls.push(`closeWindow:${desktopId}:${windowId}`);
    this.refuse();
    const desktop = this.desktop(desktopId);
    this.replace({ ...desktop, windows: desktop.windows.filter((candidate) => candidate.id !== windowId) });
    // As the shell does: the window leaves every client's layout of the desktop, each rewrite stamped.
    for (const [key, layout] of [...this.layouts]) {
      const [layoutDesktopId, clientId] = key.split("/");
      const before = { ...layout, window_paths: {} };
      const dropped = withoutPlacement(before, windowId);
      if (layoutDesktopId === desktopId && dropped !== before) this.writeLayout(desktopId, clientId, dropped);
    }
  }

  async reportWindowLocation(
    desktopId: string,
    windowId: string,
    clientId: string,
    path: string,
    title: string,
  ): Promise<WindowRecord> {
    this.calls.push(`reportWindowLocation:${desktopId}:${windowId}:${clientId}:${path}:${title}`);
    this.refuse();
    const desktop = this.desktop(desktopId);
    const window = desktop.windows.find((candidate) => candidate.id === windowId);
    if (window === undefined) throw new Error(`No window ${windowId}`);
    // As the shell does: an independent window's report is the reporting client's alone; the record keeps its home path.
    if (window.scope === "independent") {
      this.windowPaths.set(`${clientId}/${windowId}`, { path, title });
      return { ...window, path, title };
    }
    const updated = { ...window, path, title, is_settling: false };
    this.replace({
      ...desktop,
      windows: desktop.windows.map((candidate) => (candidate.id === windowId ? updated : candidate)),
    });
    return updated;
  }

  async fetchPlacements(desktopId: string, clientId: string): Promise<Layout> {
    this.calls.push(`fetchPlacements:${desktopId}`);
    this.refuse();
    return this.layoutOf(desktopId, clientId);
  }

  async savePlacements(desktopId: string, request: PlacementsSaveRequest): Promise<string | null> {
    this.calls.push(`savePlacements:${desktopId}:${request.saveId}`);
    this.refuse();
    const stored = this.layouts.get(`${desktopId}/${request.clientId}`);
    if (
      stored !== undefined &&
      stored.updated_at !== null &&
      (request.baseUpdatedAt === null || stored.updated_at > request.baseUpdatedAt)
    ) {
      throw new StalePlacementsSaveError("the stored layout is newer than the one this save was based on");
    }
    return this.writeLayout(desktopId, request.clientId, { updated_at: null, placements: request.placements })
      .updated_at;
  }

  async fetchClients(): Promise<ClientRecord[]> {
    this.calls.push("fetchClients");
    this.refuse();
    return [...this.clients];
  }

  async setAppLifecycle(appName: string, action: "stop" | "start"): Promise<void> {
    this.calls.push(`setAppLifecycle:${appName}:${action}`);
    this.refuse();
  }

  async setEntryPresentation(clientId: string, app: string, presentation: EntryPresentation): Promise<ClientRecord> {
    const position = presentation.position === null ? "-" : `${presentation.position.x},${presentation.position.y}`;
    this.calls.push(`setEntryPresentation:${clientId}:${app}:${presentation.mode}:${presentation.style}:${position}`);
    this.refuse();
    const existing = this.clients.find((client) => client.id === clientId) ?? clientRecord(clientId);
    const updated = { ...existing, entries: { ...existing.entries, [app]: presentation } };
    this.clients = [...this.clients.filter((client) => client.id !== clientId), updated];
    return updated;
  }

  async fetchAvatars(): Promise<AvatarCatalog> {
    this.calls.push("fetchAvatars");
    this.refuse();
    return this.avatars;
  }

  async selectAvatar(design: string): Promise<void> {
    this.calls.push(`selectAvatar:${design}`);
    this.refuse();
    if (!this.avatars.designs.some((candidate) => candidate.id === design)) throw new Error(`No design ${design}`);
    this.avatars = { ...this.avatars, selected: design };
  }
}

export class FakeDesktopSocket implements DesktopSocket {
  handlers: SocketHandlers | null = null;
  readonly reports: { activeDesktop: string; previousDesktop: string }[] = [];

  connect(handlers: SocketHandlers): void {
    this.handlers = handlers;
  }

  reportClientState(activeDesktop: string, previousDesktop: string): void {
    this.reports.push({ activeDesktop, previousDesktop });
  }

  /** The handlers the store registered; a test delivers events through them. */
  deliver(): SocketHandlers {
    if (this.handlers === null) throw new Error("the store has not connected");
    return this.handlers;
  }
}

/** Resolve every promise queued so far (the store's awaits run in microtasks). */
export async function settle(): Promise<void> {
  for (let round = 0; round < 10; round += 1) await Promise.resolve();
}
