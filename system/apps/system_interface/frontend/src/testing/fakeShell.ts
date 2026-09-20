/**
 * A fake shell for the store and page tests: the desktop routes over in-memory desktops and
 * layouts (with the shell's stamp rule, so a stale save is refused), and a socket whose events a
 * test delivers by hand. Every call is recorded so a test can assert what reached the shell.
 */

import type { DesktopApi } from "../store/DesktopStore";
import type { PlacementsSaveRequest, WindowOpenOutcome, WindowOpenRequest } from "../model/api";
import { StalePlacementsSaveError } from "../model/api";
import type {
  Desktop,
  DesktopShortcut,
  GridCell,
  Layout,
  SharingMode,
  Wallpaper,
  WindowRecord,
} from "../model/records";
import { withWindowPlacedOnOpen, withWindowRaised, withoutPlacement } from "../geometry/stack";
import type { DesktopSocket, SocketHandlers } from "../store/socket";

export class FakeDesktopApi implements DesktopApi {
  desktops: Desktop[] = [];
  clients: { id: string; active_desktop: string | null }[] = [];
  /** ``<desktop>/<client>`` -> the stored layout. */
  readonly layouts = new Map<string, Layout>();
  readonly calls: string[] = [];
  /** A refusal every route raises while set. */
  refusal: string | null = null;
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

  layoutOf(desktopId: string, clientId: string): Layout {
    return this.layouts.get(`${desktopId}/${clientId}`) ?? { updated_at: null, placements: [] };
  }

  writeLayout(desktopId: string, clientId: string, layout: Layout): Layout {
    const stamped = { ...layout, updated_at: this.stamp() };
    this.layouts.set(`${desktopId}/${clientId}`, stamped);
    return stamped;
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
      const dropped = withoutPlacement(layout, windowId);
      if (layoutDesktopId === desktopId && dropped !== layout) this.writeLayout(desktopId, clientId, dropped);
    }
  }

  async reportWindowLocation(desktopId: string, windowId: string, path: string, title: string): Promise<WindowRecord> {
    this.calls.push(`reportWindowLocation:${desktopId}:${windowId}:${path}:${title}`);
    this.refuse();
    const desktop = this.desktop(desktopId);
    const window = desktop.windows.find((candidate) => candidate.id === windowId);
    if (window === undefined) throw new Error(`No window ${windowId}`);
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

  async fetchClients(): Promise<{ id: string; active_desktop: string | null }[]> {
    this.calls.push("fetchClients");
    this.refuse();
    return [...this.clients];
  }

  async setAppLifecycle(appName: string, action: "stop" | "start"): Promise<void> {
    this.calls.push(`setAppLifecycle:${appName}:${action}`);
    this.refuse();
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
