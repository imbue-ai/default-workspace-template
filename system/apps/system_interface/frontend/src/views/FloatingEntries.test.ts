// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appRecord, avatarStateRecord, windowRecord } from "../testing/records";
import { FloatingEntries } from "./FloatingEntries";
import type { FloatingEntriesAttrs } from "./FloatingEntries";
import type { TaskbarEntry } from "../reducers/desktopState";

afterEach(unmountViews);

const buddy = appRecord("buddy", { pin: { path: "/", style: "avatar", scope: "linked", default_mode: "floating" } });

/** The fixture's entry: the pinned window of an app whose pin declares the avatar, shown plain and minimized. */
function pinnedEntry(overrides: Partial<TaskbarEntry> = {}): TaskbarEntry {
  return {
    window: windowRecord("win-9", "buddy", "/", { is_pinned: true }),
    app: buddy,
    title: "Buddy",
    isMinimized: true,
    isDetached: false,
    isFocused: false,
    isPinned: true,
    look: { mode: "floating", style: "plain", declaredStyle: "avatar", position: { x: 0.5, y: 0.5 } },
    ...overrides,
  };
}

function render(overrides: Partial<FloatingEntriesAttrs> = {}): HTMLElement {
  const attrs: FloatingEntriesAttrs = {
    avatar: avatarStateRecord(),
    desktop: { name: "Home", color: "#2f855a", glyph: 0 },
    entries: [pinnedEntry()],
    rectOf: () => ({ x: 500, y: 400, width: 56, height: 56 }),
    openMenuWindowId: null,
    onClick: vi.fn(),
    onContextMenu: vi.fn(),
    ...overrides,
  };
  const root = mountView(() => m(FloatingEntries, attrs));
  return root.querySelector("[data-floating-entries]") as HTMLElement;
}

describe("FloatingEntries", () => {
  it("draws each entry at its box with the contract's selectors, and is otherwise inert", () => {
    const onClick = vi.fn();
    const onContextMenu = vi.fn();
    const layer = render({ onClick, onContextMenu });
    expect(layer.classList.contains("pointer-events-none")).toBe(true);
    const entry = layer.querySelector('[data-pinned-entry="buddy"]') as HTMLElement;
    expect(entry.getAttribute("data-entry-mode")).toBe("floating");
    expect(entry.getAttribute("data-entry-style")).toBe("plain");
    expect(entry.getAttribute("data-minimized")).toBe("true");
    expect(entry.getAttribute("data-detached")).toBe("false");
    expect(entry.getAttribute("aria-pressed")).toBe("false");
    expect(entry.getAttribute("aria-label")).toBe("Buddy");
    expect(entry.style.left).toBe("500px");
    expect(entry.style.top).toBe("400px");
    expect(entry.style.width).toBe("56px");
    expect(entry.querySelector("svg")).not.toBeNull();
    entry.click();
    expect(onClick).toHaveBeenCalledWith("win-9");
    entry.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 30, clientY: 40 }));
    expect(onContextMenu).toHaveBeenCalledWith("win-9", 30, 40, expect.any(Element));
  });

  it("marks and dims an entry whose window is shown in its own desktop window, like a minimized one", () => {
    const layer = render({ entries: [pinnedEntry({ isMinimized: false, isDetached: true })] });
    const entry = layer.querySelector('[data-pinned-entry="buddy"]') as HTMLElement;
    expect(entry.getAttribute("data-minimized")).toBe("false");
    expect(entry.getAttribute("data-detached")).toBe("true");
    expect(entry.classList.contains("opacity-70")).toBe(true);
  });

  it("draws the avatar wearing the mood in the avatar style, marked stale when the status may be old", () => {
    const layer = render({
      entries: [
        pinnedEntry({
          isMinimized: false,
          isFocused: true,
          look: { mode: "floating", style: "avatar", declaredStyle: "avatar", position: null },
        }),
      ],
      avatar: avatarStateRecord({ design: "jelly-cat", status: { mood: "working", is_stale: true } }),
    });
    const entry = layer.querySelector('[data-pinned-entry="buddy"]') as HTMLElement;
    expect(entry.getAttribute("data-entry-style")).toBe("avatar");
    expect(entry.getAttribute("data-mood")).toBe("working");
    expect(entry.getAttribute("data-stale")).toBe("true");
    expect(entry.getAttribute("aria-label")).toBe("Buddy (status may be out of date)");
    // The launch video's framing: the desktop's squiggle in red stands in for the avatar's image.
    expect(entry.querySelector("img")).toBeNull();
    const squiggle = entry.querySelector("svg") as SVGSVGElement;
    expect(squiggle.getAttribute("stroke")).toBe("#e5322d");
  });

  it("paints nothing behind the icon in the plain style: no surface, no border, no padding", () => {
    // The icon carries its own tile, so a surface and 8px of padding here framed it in a second
    // one -- the pale edge around the drawing. What the box still owns is the shadow and the
    // corner it is cast around.
    const layer = render({ entries: [pinnedEntry({ isMinimized: false })] });
    const entry = layer.querySelector('[data-pinned-entry="buddy"]') as HTMLElement;
    for (const painted of ["border", "p-2", "bg-surface", "bg-surface/90", "rounded-2xl"]) {
      expect(entry.classList.contains(painted)).toBe(false);
    }
    for (const kept of ["border-0", "bg-transparent", "p-0", "rounded-[32%]", "shadow-raised"]) {
      expect(entry.classList.contains(kept)).toBe(true);
    }
  });

  it("draws nothing with no floating entries", () => {
    expect(render({ entries: [] }).querySelectorAll("[data-pinned-entry]")).toHaveLength(0);
  });
});
