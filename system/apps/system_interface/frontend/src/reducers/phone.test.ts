/**
 * The phone's selectors over states the reducers build: where a load lands, how the windows sheet orders every
 * desktop's windows (what this phone showed first, the pinned window once), which window a home tile shows, and
 * what the pill says.
 */
import { describe, expect, it } from "vitest";
import type { Desktop } from "../model/records";
import {
  appRecord,
  chatLikeAppRecord,
  desktopRecord,
  layoutRecord,
  placementRecord,
  windowRecord,
} from "../testing/records";
import { initialDesktopState, reduceDesktopState } from "./desktopState";
import type { DesktopEvent, DesktopState } from "./desktopState";
import {
  focusTargetOf,
  homeGridApps,
  openWindowCount,
  phoneLanding,
  phonePillOf,
  startSheetRows,
  windowsSheetRows,
} from "./phone";

const chat = chatLikeAppRecord("chat", { launcher_rank: 1 });
const docs = appRecord("docs");
const notes = appRecord("notes", { launcher_rank: 2 });
const helper = appRecord("helper", { internal: true });

/** Two desktops, each with its pinned chat window; docs has a window on both, notes one on work. */
function desktops(): Desktop[] {
  return [
    desktopRecord("home", {
      windows: [
        windowRecord("chat-home", "chat", "/", { is_pinned: true, title: "Planning the trip" }),
        windowRecord("docs-old", "docs", "/a", { opened_at: "2026-09-19T01:00:00Z" }),
      ],
    }),
    desktopRecord("work", {
      windows: [
        windowRecord("chat-work", "chat", "/", { is_pinned: true }),
        windowRecord("docs-new", "docs", "/b", { opened_at: "2026-09-19T03:00:00Z" }),
        windowRecord("notes-1", "notes", "/n", { opened_at: "2026-09-19T02:00:00Z", title: "Groceries" }),
      ],
    }),
  ];
}

function phoneState(...events: DesktopEvent[]): DesktopState {
  let state = initialDesktopState("client-1", { isPhone: true, isTouch: true });
  const steps: DesktopEvent[] = [
    { type: "apps_updated", apps: [docs, notes, chat, helper] },
    { type: "desktops_updated", desktops: desktops() },
    { type: "desktop_activated", desktopId: "home" },
    ...events,
  ];
  for (const event of steps) state = reduceDesktopState(state, event);
  return state;
}

describe("where a phone lands", () => {
  it("on the newest recorded entry that still exists, home included", () => {
    const history = ["notes-1", "gone-window", "docs-new"];
    expect(phoneLanding(phoneState({ type: "phone_history_loaded", history }))).toEqual({
      kind: "window",
      windowId: "docs-new",
    });
    expect(phoneLanding(phoneState({ type: "phone_history_loaded", history: ["docs-new", "home"] }))).toEqual({
      kind: "home",
    });
    // A closed window at the end is passed over for the entry before it.
    expect(phoneLanding(phoneState({ type: "phone_history_loaded", history: ["notes-1", "gone-window"] }))).toEqual({
      kind: "window",
      windowId: "notes-1",
    });
  });

  it("passes over the pinned window of a desktop the client is no longer on, for the one it keeps", () => {
    const history = ["chat-work"];
    expect(phoneLanding(phoneState({ type: "phone_history_loaded", history }))).toEqual({
      kind: "window",
      windowId: "chat-home",
    });
  });

  it("on the active desktop's pinned window with nothing recorded, else home", () => {
    expect(phoneLanding(phoneState())).toEqual({ kind: "window", windowId: "chat-home" });
    const unpinned = phoneState({
      type: "desktops_updated",
      desktops: [desktopRecord("home", { windows: [windowRecord("docs-old", "docs", "/a")] })],
    });
    expect(phoneLanding(unpinned)).toEqual({ kind: "home" });
  });
});

describe("the windows sheet", () => {
  it("lists what this phone showed, most recent first, then every other window newest first, the pinned one once", () => {
    const state = phoneState(
      { type: "phone_shown", shown: { kind: "window", windowId: "notes-1" } },
      { type: "phone_shown", shown: { kind: "home" } },
      { type: "phone_shown", shown: { kind: "window", windowId: "chat-home" } },
    );
    expect(windowsSheetRows(state).map((row) => row.window.id)).toEqual([
      "chat-home",
      "notes-1",
      "docs-new",
      "docs-old",
    ]);
    expect(windowsSheetRows(state).find((row) => row.window.id === "notes-1")?.title).toBe("Groceries");
    // The pill counts every listed window but the pinned one.
    expect(openWindowCount(state)).toBe(3);
  });

  it("keeps an entry once in the history, moving it to the front when shown again", () => {
    const state = phoneState(
      { type: "phone_shown", shown: { kind: "window", windowId: "docs-old" } },
      { type: "phone_shown", shown: { kind: "window", windowId: "notes-1" } },
      { type: "phone_shown", shown: { kind: "window", windowId: "docs-old" } },
    );
    expect(state.phone.history).toEqual(["notes-1", "docs-old"]);
    expect(
      windowsSheetRows(state)
        .map((row) => row.window.id)
        .slice(0, 2),
    ).toEqual(["docs-old", "notes-1"]);
  });
});

describe("the start sheet", () => {
  it("offers the windows the sheet lists, one pinned window among them, and nothing for other desktops' pinned", () => {
    const menu = startSheetRows(phoneState(), "chat");
    expect(menu.windowRows.map((row) => row.window.id)).toEqual(["chat-home"]);
    expect(menu.rows.filter((row) => row.kind === "window")).toEqual(menu.windowRows);
    // A query only another desktop's pinned window matched is no match on the phone.
    const work = phoneState({
      type: "desktops_updated",
      desktops: desktops().map((desktop) =>
        desktop.id === "work"
          ? { ...desktop, windows: desktop.windows.map((window) => ({ ...window, title: "Quarterly review" })) }
          : desktop,
      ),
    });
    expect(startSheetRows(work, "Quarterly").isNoMatch).toBe(false);
    const onlyPinned = phoneState({
      type: "desktops_updated",
      desktops: [
        desktopRecord("home", { windows: [windowRecord("chat-home", "chat", "/", { is_pinned: true })] }),
        desktopRecord("work", {
          windows: [windowRecord("chat-work", "chat", "/", { is_pinned: true, title: "Quarterly review" })],
        }),
      ],
    });
    expect(startSheetRows(onlyPinned, "Quarterly")).toMatchObject({ windowRows: [], isNoMatch: true });
  });
});

describe("a home tile", () => {
  it("shows the app's window nearest the top of this client's stack on its desktop, else its newest anywhere", () => {
    const placed = phoneState({
      type: "layout_loaded",
      desktopId: "home",
      layout: layoutRecord([placementRecord("docs-old", { is_minimized: true })]),
    });
    expect(focusTargetOf(placed, "docs")?.id).toBe("docs-old");
    // Notes has no window on the active desktop: its newest window anywhere.
    expect(focusTargetOf(placed, "notes")?.id).toBe("notes-1");
    const onlyElsewhere = phoneState({
      type: "desktops_updated",
      desktops: [
        desktopRecord("home"),
        desktopRecord("work", {
          windows: [
            windowRecord("docs-a", "docs", "/a", { opened_at: "2026-09-19T01:00:00Z" }),
            windowRecord("docs-b", "docs", "/b", { opened_at: "2026-09-19T05:00:00Z" }),
          ],
        }),
      ],
    });
    expect(focusTargetOf(onlyElsewhere, "docs")?.id).toBe("docs-b");
    expect(focusTargetOf(onlyElsewhere, "chat")).toBeNull();
  });

  it("lists every openable app in the launcher's order", () => {
    expect(homeGridApps(phoneState()).map((app) => app.name)).toEqual(["chat", "notes", "docs"]);
  });
});

describe("the pill", () => {
  it("names the workspace at home, the pinned window by its app, and any other window by its title", () => {
    const home = phoneState(
      { type: "workspace_name_updated", workspaceName: "Trip planning" },
      { type: "phone_shown", shown: { kind: "home" } },
    );
    expect(phonePillOf(home)).toEqual({ kind: "home", title: "Trip planning" });
    expect(phonePillOf(phoneState({ type: "phone_shown", shown: { kind: "home" } })).title).toBe("Workspace");
    const pinned = phonePillOf(phoneState({ type: "phone_shown", shown: { kind: "window", windowId: "chat-home" } }));
    expect(pinned.title).toBe("Chat");
    const notesPill = phonePillOf(phoneState({ type: "phone_shown", shown: { kind: "window", windowId: "notes-1" } }));
    expect(notesPill.title).toBe("Groceries");
  });
});
