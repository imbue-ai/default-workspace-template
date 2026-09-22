/**
 * Record factories for the frontend tests: an app as the shell lists it (and one shaped like the
 * chat's manifest), a desktop, a window, a placement, a launch path, a client record, a layout, the
 * avatar state, the theme metrics, and the update notice as the shell sends it. Each takes
 * overrides so a test spells only what it is about.
 */

import type {
  AppRecord,
  ClientRecord,
  Desktop,
  LaunchPath,
  Layout,
  Placement,
  UpdateNoticeWire,
  WindowRecord,
} from "../model/records";
import { cascadeFrame } from "../geometry/frames";
import type { ThemeMetrics } from "../theme/metrics";
import type { AvatarState } from "../reducers/desktopState";

function capitalized(name: string): string {
  return name.charAt(0).toUpperCase() + name.slice(1);
}

/** A launch path ``new`` at ``/new`` with no params. */
export function launchPathRecord(overrides: Partial<LaunchPath> = {}): LaunchPath {
  return { id: "new", label: "New", path: "/new", params: [], text_param: null, ...overrides };
}

/** A running, non-critical, supervised app with one ``new`` launch path at ``/new``. */
export function appRecord(name: string, overrides: Partial<AppRecord> = {}): AppRecord {
  return {
    name,
    display_name: capitalized(name),
    icon: "",
    label: "",
    url: `http://127.0.0.1:1/${name}`,
    internal: false,
    program: name,
    critical: false,
    launch_paths: [launchPathRecord({ label: `New ${name}` })],
    default_shortcut: { launch: "new", mode: "focus" },
    launcher_rank: null,
    pin: null,
    message_handlers: [],
    is_running: true,
    ...overrides,
  };
}

/** An app shaped like the chat's manifest: ranked, an independent avatar pin at ``/``, a ``root`` launch path at
 *  the pin's path taking a ``draft``, and ``new`` and ``send`` launch paths taking ``message`` as typed text. */
export function chatLikeAppRecord(name: string, overrides: Partial<AppRecord> = {}): AppRecord {
  const displayName = capitalized(name);
  return appRecord(name, {
    launcher_rank: 10,
    pin: { path: "/", style: "avatar", scope: "independent", default_mode: "floating" },
    launch_paths: [
      launchPathRecord({ id: "root", label: displayName, path: "/", params: ["draft"] }),
      launchPathRecord({
        id: "new",
        label: `New ${displayName}`,
        path: "/new",
        params: ["message"],
        text_param: "message",
      }),
      launchPathRecord({
        id: "send",
        label: `Send to ${name}...`,
        path: "/send",
        params: ["message"],
        text_param: "message",
      }),
    ],
    ...overrides,
  });
}

/** A settled window of ``app`` at ``path`` with an empty title. */
export function windowRecord(
  id: string,
  app: string,
  path: string,
  overrides: Partial<WindowRecord> = {},
): WindowRecord {
  return {
    id,
    app,
    path,
    title: "",
    opened_at: "2026-09-19T00:00:00Z",
    is_settling: false,
    is_pinned: false,
    scope: "linked",
    ...overrides,
  };
}

/** A shared desktop named after its id, with no wallpaper, shortcuts, or windows. */
export function desktopRecord(id: string, overrides: Partial<Desktop> = {}): Desktop {
  return {
    id,
    name: capitalized(id),
    color: "#2f6b4f",
    glyph: 0,
    sharing: "shared",
    wallpaper: null,
    shortcuts: [],
    windows: [],
    ...overrides,
  };
}

/** A connected client on no desktop yet, with no entry presentations. */
export function clientRecord(id: string, overrides: Partial<ClientRecord> = {}): ClientRecord {
  return {
    id,
    active_desktop: null,
    last_seen: "2026-09-19T00:00:00Z",
    is_connected: true,
    entries: {},
    ...overrides,
  };
}

/** The default design, idle and fresh. */
export function avatarStateRecord(overrides: Partial<AvatarState> = {}): AvatarState {
  return {
    design: "gummy-seal",
    defaultDesign: "gummy-seal",
    status: { mood: "idle", is_stale: false },
    ...overrides,
  };
}

/** A layout of ``placements`` with the stamp ``updatedAt`` and no stored window paths. */
export function layoutRecord(placements: readonly Placement[], updatedAt: string | null = null): Layout {
  return { updated_at: updatedAt, placements, window_paths: {} };
}

/** A shown, normal placement at the first cascade frame. */
export function placementRecord(windowId: string, overrides: Partial<Placement> = {}): Placement {
  return { window_id: windowId, frame: cascadeFrame(0), state: "NORMAL", is_minimized: false, ...overrides };
}

/** An open notice (no rollback started) for an apply that touched ``apps``, one program each. */
export function noticeWire(apps: string[], overrides: Partial<UpdateNoticeWire> = {}): UpdateNoticeWire {
  return {
    merge_sha: "abc1234abc1234abc1234abc1234abc1234abc12",
    applied_at: 1_780_000_000,
    driven_by: "mngr/update-widgets",
    apps,
    programs: apps,
    needs_system_services_restart: false,
    progress: null,
    outcome: null,
    ...overrides,
  };
}

/** The theme metrics at the contract's default (non-compact) values. */
export function themeMetricsRecord(overrides: Partial<ThemeMetrics> = {}): ThemeMetrics {
  return {
    titleBarHeight: 36,
    taskbarHeight: 48,
    cellWidth: 96,
    cellHeight: 112,
    gridInset: 16,
    windowMinWidth: 320,
    windowMinHeight: 240,
    titleMinVisible: 120,
    snapThreshold: 16,
    unsnapDistance: 12,
    dragThreshold: 4,
    touchTarget: 32,
    floatingEntrySize: 56,
    floatingEntryInsetX: 16,
    floatingEntryInsetY: 12,
    ...overrides,
  };
}
