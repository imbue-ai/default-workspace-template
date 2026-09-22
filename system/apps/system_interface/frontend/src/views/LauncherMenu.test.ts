// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appRecord, chatLikeAppRecord, desktopRecord, launchPathRecord, windowRecord } from "../testing/records";
import { initialDesktopState, reduceDesktopState } from "../reducers/desktopState";
import type { DesktopState } from "../reducers/desktopState";
import { defaultHighlightIndex, launcherRowsOf } from "../reducers/launcherRows";
import type { LauncherMenuRows } from "../reducers/launcherRows";
import { LauncherMenu, secondaryKeyLabel, textPreview } from "./LauncherMenu";
import type { LauncherMenuAttrs } from "./LauncherMenu";

afterEach(unmountViews);

const chatty = chatLikeAppRecord("chatty");
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
    bottomOffsetPx: 0,
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
  it("draws the launch-path rows then the primary text row, the first highlighted and wearing Enter", () => {
    const { root } = render(launcherRowsOf(stateWithWindows(), ""));
    expect(root.querySelector("[data-launcher-overlay]")).not.toBeNull();
    expect(rowKeys(root)).toEqual(["launch:chatty:root", "launch:terminal:new", "text:chatty:new"]);
    const first = root.querySelector('[data-launch="chatty:root"]')!;
    expect(first.getAttribute("data-highlighted")).toBe("true");
    expect(first.querySelector('[data-key="enter"]')!.textContent).toBe("Enter");
    expect(root.querySelector('[data-launch="terminal:new"]')!.getAttribute("data-highlighted")).toBe("false");
    expect(root.querySelector('[data-text-action="primary"]')!.getAttribute("data-launch")).toBe("chatty:new");
    // The Enter caption is the highlight's alone; nothing typed, so no secondary row and no window rows.
    expect(root.querySelectorAll('[data-key="enter"]')).toHaveLength(1);
    expect(root.querySelector('[data-text-action="secondary"]')).toBeNull();
    expect(root.querySelector("[data-launcher-window]")).toBeNull();
    expect(root.querySelector(".launcher-no-matches")).toBeNull();
  });

  it("sits above the field: its foot is lifted by the field's rise", () => {
    const grounded = render(launcherRowsOf(stateWithWindows(), ""));
    expect((grounded.root.querySelector("[data-launcher-overlay]") as HTMLElement).style.bottom).toBe("0px");
    unmountViews();
    const lifted = render(launcherRowsOf(stateWithWindows(), ""), { bottomOffsetPx: 140 });
    expect((lifted.root.querySelector("[data-launcher-overlay]") as HTMLElement).style.bottom).toBe("140px");
  });

  it("the Enter caption follows a moved highlight; the secondary row always wears its chord", () => {
    const menu = launcherRowsOf(stateWithWindows(), "shell");
    const { root } = render(menu, { highlightIndex: menu.rows.length - 1 });
    const secondary = root.querySelector('[data-text-action="secondary"]')!;
    expect(secondary.getAttribute("data-highlighted")).toBe("true");
    expect(secondary.textContent).toContain("Ctrl+Enter");
    expect(secondary.querySelector('[data-key="enter"]')).not.toBeNull();
    expect(root.querySelector('[data-text-action="primary"]')!.querySelector('[data-key="enter"]')).toBeNull();
    expect(root.querySelectorAll('[data-key="enter"]')).toHaveLength(1);
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
    // The note stands in for the empty sections, so it opens the card and the divider sits between it and the rows.
    const textSection = noMatch.root.querySelector('[data-section="text"]')!;
    expect(textSection.children[0].className).toContain("launcher-no-matches");
    expect(textSection.children[1].className).toContain("border-t");
    expect(textSection.children[2].getAttribute("data-text-action")).toBe("primary");
  });

  it("a click runs an enabled row and a hover moves the highlight; a disabled row runs nothing and takes no hover", () => {
    const { root, attrs } = render(launcherRowsOf(stateWithWindows(), ""));
    (root.querySelector('[data-launch="terminal:new"]') as HTMLElement).click();
    expect(attrs.onRun).toHaveBeenCalledWith(expect.objectContaining({ key: "launch:terminal:new" }));
    root.querySelector('[data-text-action="primary"]')!.dispatchEvent(new PointerEvent("pointerenter"));
    expect(attrs.onHighlight).toHaveBeenCalledWith(2);
    unmountViews();
    const tooLong = render(launcherRowsOf(stateWithWindows(), "x".repeat(2100)));
    const disabled = tooLong.root.querySelector('[data-text-action="secondary"]') as HTMLElement;
    expect(disabled.getAttribute("data-disabled")).toBe("true");
    disabled.click();
    disabled.dispatchEvent(new PointerEvent("pointerenter"));
    expect(tooLong.attrs.onRun).not.toHaveBeenCalled();
    expect(tooLong.attrs.onHighlight).not.toHaveBeenCalled();
  });

  it("spells the secondary key for the platform and previews the first words of the text", () => {
    expect(secondaryKeyLabel(true)).toBe("Cmd+Enter");
    expect(secondaryKeyLabel(false)).toBe("Ctrl+Enter");
    expect(textPreview("")).toBe("");
    expect(textPreview("plan the launch")).toBe("plan the launch");
    expect(textPreview("one two three four five six")).toBe("one two three four…");
  });
});
