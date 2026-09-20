import { describe, expect, it } from "vitest";
import {
  WireShapeError,
  isSameWindowPaths,
  parseAppRecord,
  parseClientRecords,
  parseDesktop,
  parseLayout,
  parseClientRecord,
  parseWallpaperListings,
  shortcutKey,
} from "./records";

const DESKTOP_WIRE = {
  id: "home",
  name: "Home",
  color: "#2f6b4f",
  glyph: 0,
  sharing: "shared",
  wallpaper: { kind: "bundled", name: "dawn" },
  shortcuts: [{ target: { kind: "launch", app: "docs", launch: "new" }, mode: "new", cell: { column: 0, row: 0 } }],
  windows: [
    {
      id: "win-0123456789abcdef",
      app: "docs",
      path: "/?doc=1",
      title: "Plan",
      opened_at: "2026-09-19T14:11:02.824Z",
      is_settling: false,
    },
  ],
};

describe("parseDesktop", () => {
  it("reads the contract's desktop object", () => {
    const desktop = parseDesktop(DESKTOP_WIRE);
    expect(desktop.wallpaper).toEqual({ kind: "bundled", name: "dawn" });
    expect(desktop.shortcuts[0].target).toEqual({ kind: "launch", app: "docs", launch: "new" });
    expect(desktop.windows[0].path).toBe("/?doc=1");
  });

  it("reads a V1 window record with the pin fields defaulted, and a pinned window's own", () => {
    const desktop = parseDesktop(DESKTOP_WIRE);
    expect(desktop.windows[0].is_pinned).toBe(false);
    expect(desktop.windows[0].scope).toBe("linked");
    const pinned = parseDesktop({
      ...DESKTOP_WIRE,
      windows: [{ ...DESKTOP_WIRE.windows[0], is_pinned: true, scope: "independent" }],
    });
    expect(pinned.windows[0].is_pinned).toBe(true);
    expect(pinned.windows[0].scope).toBe("independent");
    expect(() =>
      parseDesktop({ ...DESKTOP_WIRE, windows: [{ ...DESKTOP_WIRE.windows[0], scope: "personal" }] }),
    ).toThrow(WireShapeError);
  });

  it("reads a null wallpaper and refuses a desktop of the wrong shape", () => {
    expect(parseDesktop({ ...DESKTOP_WIRE, wallpaper: null }).wallpaper).toBeNull();
    expect(() => parseDesktop({ ...DESKTOP_WIRE, windows: "none" })).toThrow(WireShapeError);
    expect(() => parseDesktop({ ...DESKTOP_WIRE, sharing: "public" })).toThrow(WireShapeError);
    expect(() => parseDesktop([])).toThrow(WireShapeError);
  });
});

describe("parseLayout", () => {
  it("reads the placements back to front with their stamp", () => {
    const layout = parseLayout({
      version: 1,
      updated_at: "2026-09-19T14:12:40.001Z",
      placements: [
        {
          window_id: "win-1",
          frame: { x: 0.05, y: 0.06, width: 0.6, height: 0.7 },
          state: "NORMAL",
          is_minimized: false,
        },
      ],
    });
    expect(layout.updated_at).toBe("2026-09-19T14:12:40.001Z");
    expect(layout.placements[0].state).toBe("NORMAL");
    // A layout with no ``window_paths`` (the save route's echo) reads as one with none.
    expect(parseLayout({ version: 1, updated_at: null, placements: [] })).toEqual({
      updated_at: null,
      placements: [],
      window_paths: {},
    });
  });

  it("reads the client's stored paths for independent windows, and tells two sets apart", () => {
    const layout = parseLayout({
      version: 1,
      updated_at: null,
      placements: [],
      window_paths: { "win-1": { path: "/?doc=2", title: "Second" } },
    });
    expect(layout.window_paths).toEqual({ "win-1": { path: "/?doc=2", title: "Second" } });
    expect(() => parseLayout({ updated_at: null, placements: [], window_paths: { "win-1": { path: 3 } } })).toThrow(
      WireShapeError,
    );
    expect(isSameWindowPaths(layout.window_paths, { "win-1": { path: "/?doc=2", title: "Second" } })).toBe(true);
    expect(isSameWindowPaths(layout.window_paths, { "win-1": { path: "/?doc=2", title: "Other" } })).toBe(false);
    expect(isSameWindowPaths(layout.window_paths, {})).toBe(false);
    expect(isSameWindowPaths({}, {})).toBe(true);
  });

  it("refuses a placement in an unknown state", () => {
    expect(() =>
      parseLayout({
        updated_at: null,
        placements: [
          { window_id: "win-1", frame: { x: 0, y: 0, width: 1, height: 1 }, state: "TILED", is_minimized: false },
        ],
      }),
    ).toThrow(WireShapeError);
  });
});

describe("parseAppRecord", () => {
  it("reads the app object with its launch paths and defaults the optional fields", () => {
    const app = parseAppRecord({
      name: "docs",
      url: "http://127.0.0.1:1",
      launch_paths: [{ id: "new", label: "New docs", path: "/new", params: ["message"] }],
      default_shortcut: { action: "new", launch: "new", mode: "new" },
      launcher_rank: 10,
      is_running: true,
    });
    expect(app.display_name).toBe("docs");
    expect(app.icon).toBe("");
    expect(app.launch_paths[0].params).toEqual(["message"]);
    expect(app.default_shortcut).toEqual({ launch: "new", mode: "new" });
    expect(app.launcher_rank).toBe(10);
    expect(app.critical).toBe(false);
    expect(app.pin).toBeNull();
  });

  it("reads an app's pin and refuses one outside the vocabularies", () => {
    const wire = { name: "docs", url: "http://127.0.0.1:1" };
    const pinned = parseAppRecord({
      ...wire,
      pin: { path: "/", style: "avatar", scope: "independent", default_mode: "floating" },
    });
    expect(pinned.pin).toEqual({ path: "/", style: "avatar", scope: "independent", default_mode: "floating" });
    expect(() =>
      parseAppRecord({ ...wire, pin: { path: "/", style: "dot", scope: "linked", default_mode: "bar" } }),
    ).toThrow(WireShapeError);
  });

  it("reads a focus shortcut and a null rank", () => {
    const app = parseAppRecord({
      name: "files",
      url: "http://127.0.0.1:2",
      default_shortcut: { launch: "new", mode: "focus" },
      launcher_rank: null,
    });
    expect(app.default_shortcut).toEqual({ launch: "new", mode: "focus" });
    expect(app.launcher_rank).toBeNull();
    expect(app.launch_paths).toEqual([]);
  });
});

describe("the small helpers", () => {
  it("read a client, spell a shortcut key, and compare frames", () => {
    expect(parseClientRecord({ id: "c1", active_desktop: null, last_seen: "now", is_connected: true })).toEqual({
      id: "c1",
      active_desktop: null,
      last_seen: "now",
      is_connected: true,
    });
    expect(shortcutKey("docs", "new")).toBe("docs:new");
  });

  it("refuses a clients or wallpapers document missing its list instead of reading it as empty", () => {
    expect(parseClientRecords([])).toEqual([]);
    expect(() => parseClientRecords(undefined)).toThrow(WireShapeError);
    expect(parseWallpaperListings([{ kind: "bundled", name: "dawn", url: "/wallpapers/bundled/dawn" }])).toHaveLength(
      1,
    );
    expect(() => parseWallpaperListings({})).toThrow(WireShapeError);
  });
});
