import { describe, expect, it } from "vitest";
import {
  appRecord,
  chatLikeAppRecord,
  desktopRecord,
  getMethodFreeTextAppRecord,
  launchPathRecord,
  layoutRecord,
  placementRecord,
  windowRecord,
} from "../testing/records";
import { desktopStateWithApps } from "../testing/states";
import { initialDesktopState, reduceDesktopState } from "./desktopState";
import type { DesktopState } from "./desktopState";
import {
  defaultHighlightIndex,
  isMessageText,
  isRowEnabled,
  launcherRowsOf,
  moveHighlight,
  secondaryTextRow,
} from "./launcherRows";

const MODES = { isCompact: false, isTouch: false };

const chatty = chatLikeAppRecord("chatty");
const terminal = appRecord("terminal", {
  launcher_rank: 40,
  launch_paths: [launchPathRecord({ id: "new", label: "New Terminal", path: "/new" })],
});
const hidden = appRecord("hidden", { internal: true });

function state(): DesktopState {
  let next = initialDesktopState("client-1", MODES);
  next = reduceDesktopState(next, { type: "apps_updated", apps: [terminal, chatty, hidden] });
  next = reduceDesktopState(next, {
    type: "desktops_updated",
    desktops: [
      desktopRecord("home", {
        windows: [
          windowRecord("win-1", "chatty", "/", { is_pinned: true, scope: "independent" }),
          windowRecord("win-2", "terminal", "/?session=terminal-1", { title: "terminal-1" }),
        ],
      }),
      desktopRecord("work", {
        name: "Work",
        windows: [windowRecord("win-3", "terminal", "/?session=build", { title: "build" })],
      }),
    ],
  });
  next = reduceDesktopState(next, { type: "desktop_activated", desktopId: "home" });
  return reduceDesktopState(next, {
    type: "layout_loaded",
    desktopId: "home",
    layout: layoutRecord([placementRecord("win-2", { is_minimized: true })]),
  });
}

function keys(rows: readonly { key: string }[]): string[] {
  return rows.map((row) => row.key);
}

describe("the launcher's rows", () => {
  it("with no query lists the launch-path rows (less the free-text ones) and the primary text row, no windows", () => {
    const menu = launcherRowsOf(state(), "");
    expect(keys(menu.rows)).toEqual(["launch:chatty:root", "launch:terminal:new", "text:chatty:new"]);
    expect(menu.launchRows[1].caption).toBe("Terminal");
    expect(menu.launchRows[0].caption).toBeNull();
    // Nothing typed: the primary runs the bare launch path; the secondary has nothing to send, so it is not offered.
    expect(menu.textRows.map((row) => row.textAction)).toEqual(["primary"]);
    expect(menu.textRows[0].disabledReason).toBeNull();
    expect(menu.isNoMatch).toBe(false);
    expect(defaultHighlightIndex(menu.rows)).toBe(0);
  });

  it("with a line break in the text offers the free-text rows alone: the text is a message, not a query", () => {
    expect(isMessageText("plan\nthe launch")).toBe(true);
    expect(isMessageText("plan the launch")).toBe(false);
    const menu = launcherRowsOf(state(), "term\nand more");
    expect(keys(menu.rows)).toEqual(["text:chatty:new", "text:chatty:send", "text:chatty:draft"]);
    expect(menu.isNoMatch).toBe(false);
    expect(menu.textRows[0].text).toBe("term\nand more");
    expect(defaultHighlightIndex(menu.rows)).toBe(0);
    // The break just typed, with nothing after it yet, already makes the message; the rows send the trimmed text.
    const justBroken = launcherRowsOf(state(), "term\n");
    expect(keys(justBroken.rows)).toEqual(["text:chatty:new", "text:chatty:send", "text:chatty:draft"]);
    expect(justBroken.isNoMatch).toBe(false);
    expect(justBroken.textRows[0].text).toBe("term");
  });

  it("with a query keeps matching launch paths and windows across desktops, and the free-text rows always", () => {
    const menu = launcherRowsOf(state(), "term");
    expect(keys(menu.rows)).toEqual([
      "launch:terminal:new",
      "window:win-2",
      "window:win-3",
      "text:chatty:new",
      "text:chatty:send",
      "text:chatty:draft",
    ]);
    expect(menu.windowRows[0].isMinimized).toBe(true);
    expect(menu.windowRows[0].isOnActiveDesktop).toBe(true);
    expect(menu.windowRows[1].desktopName).toBe("Work");
    expect(menu.windowRows[1].isOnActiveDesktop).toBe(false);
    // A desktop's name finds its windows too.
    expect(keys(launcherRowsOf(state(), "work").windowRows)).toEqual(["window:win-3"]);
    expect(menu.textRows.every((row) => row.text === "term" && row.disabledReason === null)).toBe(true);
    expect(menu.textRows[0].disabledReason).toBeNull();
  });

  it("with no match highlights the primary text action, whose text is what was typed", () => {
    const menu = launcherRowsOf(state(), "  new chat  ");
    expect(menu.isNoMatch).toBe(true);
    expect(keys(menu.rows)).toEqual(["text:chatty:new", "text:chatty:send", "text:chatty:draft"]);
    expect(defaultHighlightIndex(menu.rows)).toBe(0);
    expect(menu.rows[0]).toMatchObject({ kind: "text", text: "new chat" });
    expect(secondaryTextRow(menu.rows)?.key).toBe("text:chatty:send");
    // The third free-text row (the draft) has no key binding.
    expect(menu.textRows[2].textAction).toBeNull();
  });

  it("stands a GET free-text row down over the path bound, never a POST one, and has no secondary with nothing typed", () => {
    const posted = launcherRowsOf(state(), "x".repeat(2100));
    // The chat-like app's rows are POST launch paths: the text rides in a body, so nothing stands down.
    expect(posted.textRows.every((row) => row.disabledReason === null)).toBe(true);
    expect(secondaryTextRow(launcherRowsOf(state(), "").rows)).toBeNull();
    // A GET free-text row is bounded by the page path it would open at.
    const gettable = desktopStateWithApps([getMethodFreeTextAppRecord("noting", ["new"])]);
    const menu = launcherRowsOf(gettable, "x".repeat(2100));
    expect(menu.textRows.every((row) => row.disabledReason === "Too long to send from here")).toBe(true);
    expect(defaultHighlightIndex(menu.rows)).toBe(-1);
    expect(secondaryTextRow(menu.rows)).toBeNull();
    expect(moveHighlight(menu.rows, 0, 1)).toBe(-1);
  });

  it("moves the highlight through the enabled rows, wrapping, from the default when nothing was moved", () => {
    const { rows } = launcherRowsOf(state(), "");
    expect(rows.map(isRowEnabled)).toEqual([true, true, true]);
    expect(moveHighlight(rows, -1, 1)).toBe(1);
    expect(moveHighlight(rows, 1, 1)).toBe(2);
    expect(moveHighlight(rows, 2, 1)).toBe(0);
    expect(moveHighlight(rows, 0, -1)).toBe(2);
  });

  it("lists nothing but a note when no app is registered", () => {
    const empty = reduceDesktopState(initialDesktopState("client-1", MODES), { type: "apps_updated", apps: [] });
    const menu = launcherRowsOf(empty, "");
    expect(menu.rows).toEqual([]);
    expect(defaultHighlightIndex(menu.rows)).toBe(-1);
  });
});
