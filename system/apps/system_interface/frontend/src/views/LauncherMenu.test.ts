// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "../testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appRecord, desktopRecord, launchPathRecord, windowRecord } from "../testing/records";
import { initialDesktopState, reduceDesktopState } from "../reducers/desktopState";
import type { DesktopState } from "../reducers/desktopState";
import { defaultHighlightIndex, launcherRowsOf } from "../reducers/launcherRows";
import type { LauncherMenuRows } from "../reducers/launcherRows";
import { LauncherMenu, secondaryKeyLabel, textPreview } from "./LauncherMenu";
import type { LauncherMenuAttrs } from "./LauncherMenu";

afterEach(unmountViews);

const chatty = appRecord("chatty", {
  launcher_rank: 10,
  pin: { path: "/", style: "avatar", scope: "independent", default_mode: "floating" },
  launch_paths: [
    launchPathRecord({ id: "root", label: "Chatty", path: "/" }),
    launchPathRecord({ id: "new", label: "New Chatty", path: "/new", params: ["message"], text_param: "message" }),
    launchPathRecord({
      id: "send",
      label: "Send to chatty...",
      path: "/send",
      params: ["message"],
      text_param: "message",
    }),
  ],
});
const terminal = appRecord("terminal", { launch_paths: [launchPathRecord({ id: "new", label: "New Terminal" })] });

function stateWithWindows(): DesktopState {
  let next = initialDesktopState("client-1", { isCompact: false, isTouch: false });
  next = reduceDesktopState(next, { type: "apps_updated", apps: [terminal, chatty] });
  next = reduceDesktopState(next, {
    type: "desktops_updated",
    desktops: [
      desktopRecord("home", { windows: [windowRecord("win-2", "terminal", "/?session=t", { title: "shell" })] }),
    ],
  });
  return reduceDesktopState(next, { type: "desktop_activated", desktopId: "home" });
}

function render(
  menu: LauncherMenuRows,
  overrides: Partial<LauncherMenuAttrs> = {},
): { root: HTMLElement; attrs: LauncherMenuAttrs } {
  const attrs: LauncherMenuAttrs = {
    menu,
    highlightIndex: defaultHighlightIndex(menu.rows),
    isCompact: false,
    isApplePlatform: false,
    onRun: vi.fn(),
    onHighlight: vi.fn(),
    ...overrides,
  };
  return { root: mountView(() => m(LauncherMenu, attrs)), attrs };
}

function rowKeys(root: HTMLElement): string[] {
  return Array.from(root.querySelectorAll("[data-launcher-row]")).map((row) => row.getAttribute("data-launcher-row")!);
}

describe("the launcher menu", () => {
  it("draws the launch-path rows then the free-text rows, the first highlighted, with its markers", () => {
    const { root } = render(launcherRowsOf(stateWithWindows(), ""));
    expect(root.querySelector("[data-launcher-overlay]")).not.toBeNull();
    expect(rowKeys(root)).toEqual([
      "launch:chatty:root",
      "launch:terminal:new",
      "text:chatty:new",
      "text:chatty:send",
    ]);
    expect(root.querySelector('[data-launch="chatty:root"]')!.getAttribute("data-highlighted")).toBe("true");
    expect(root.querySelector('[data-launch="terminal:new"]')!.getAttribute("data-highlighted")).toBe("false");
    expect(root.querySelector('[data-text-action="primary"]')!.getAttribute("data-launch")).toBe("chatty:new");
    expect(root.querySelector('[data-text-action="primary"]')!.textContent).toContain("Enter");
    // Nothing typed: the secondary stands down with its reason, and the window rows are absent.
    const secondary = root.querySelector('[data-text-action="secondary"]')!;
    expect(secondary.getAttribute("data-disabled")).toBe("true");
    expect(secondary.textContent).toContain("Ctrl+Enter");
    expect(root.querySelector("[data-launcher-window]")).toBeNull();
    expect(root.querySelector(".launcher-no-matches")).toBeNull();
  });

  it("while typing shows the matching windows and repeats the text on the free-text rows; with no match, says so", () => {
    const { root } = render(launcherRowsOf(stateWithWindows(), "shell"));
    expect(root.querySelector('[data-launcher-window="win-2"]')).not.toBeNull();
    expect(root.querySelector('[data-text-action="primary"]')!.textContent).toContain("“shell”");
    expect(root.querySelector('[data-text-action="secondary"]')!.getAttribute("data-disabled")).toBeNull();
    unmountViews();
    const noMatch = render(launcherRowsOf(stateWithWindows(), "zzzz"));
    expect(noMatch.root.querySelector(".launcher-no-matches")!.textContent).toBe("No apps or windows match");
    expect(noMatch.root.querySelector('[data-text-action="primary"]')!.getAttribute("data-highlighted")).toBe("true");
  });

  it("a click runs an enabled row and a hover moves the highlight; a disabled row runs nothing", () => {
    const { root, attrs } = render(launcherRowsOf(stateWithWindows(), ""));
    (root.querySelector('[data-launch="terminal:new"]') as HTMLElement).click();
    expect(attrs.onRun).toHaveBeenCalledWith(expect.objectContaining({ key: "launch:terminal:new" }));
    root.querySelector('[data-text-action="primary"]')!.dispatchEvent(new PointerEvent("pointerenter"));
    expect(attrs.onHighlight).toHaveBeenCalledWith(2);
    (root.querySelector('[data-text-action="secondary"]') as HTMLElement).click();
    expect(attrs.onRun).toHaveBeenCalledTimes(1);
  });

  it("spells the secondary key for the platform and previews the first words of the text", () => {
    expect(secondaryKeyLabel(true)).toBe("Cmd+Enter");
    expect(secondaryKeyLabel(false)).toBe("Ctrl+Enter");
    expect(textPreview("")).toBe("");
    expect(textPreview("plan the launch")).toBe("plan the launch");
    expect(textPreview("one two three four five six")).toBe("one two three four…");
  });
});
