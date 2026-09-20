// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "../testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appRecord, windowRecord } from "../testing/records";
import { FloatingEntries } from "./FloatingEntries";
import type { FloatingEntriesAttrs } from "./FloatingEntries";

afterEach(unmountViews);

function render(overrides: Partial<FloatingEntriesAttrs> = {}): HTMLElement {
  const buddy = appRecord("buddy", { pin: { path: "/", style: "avatar", scope: "linked", default_mode: "floating" } });
  const attrs: FloatingEntriesAttrs = {
    entries: [
      {
        window: windowRecord("win-9", "buddy", "/", { is_pinned: true }),
        app: buddy,
        title: "Buddy",
        isMinimized: true,
        isFocused: false,
        isPinned: true,
        look: { mode: "floating", style: "plain", declaredStyle: "avatar", position: { x: 0.5, y: 0.5 } },
      },
    ],
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
    expect(entry.getAttribute("aria-pressed")).toBe("false");
    expect(entry.getAttribute("aria-label")).toBe("Buddy");
    expect(entry.style.left).toBe("500px");
    expect(entry.style.top).toBe("400px");
    expect(entry.style.width).toBe("56px");
    expect(entry.querySelector("svg")).not.toBeNull();
    entry.click();
    expect(onClick).toHaveBeenCalledWith("win-9");
    entry.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, clientX: 30, clientY: 40 }));
    expect(onContextMenu).toHaveBeenCalledWith("win-9", 30, 40);
  });

  it("draws nothing with no floating entries", () => {
    expect(render({ entries: [] }).querySelectorAll("[data-pinned-entry]")).toHaveLength(0);
  });
});
