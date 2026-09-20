import { describe, expect, it } from "vitest";
import {
  WireShapeError,
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
    expect(parseLayout({ version: 1, updated_at: null, placements: [] })).toEqual({
      updated_at: null,
      placements: [],
    });
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
  });

  it("reads a default shortcut with no launch and a null rank", () => {
    const app = parseAppRecord({
      name: "files",
      url: "http://127.0.0.1:2",
      default_shortcut: { action: "open", launch: null, mode: "focus" },
      launcher_rank: null,
    });
    expect(app.default_shortcut).toEqual({ launch: null, mode: "focus" });
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
