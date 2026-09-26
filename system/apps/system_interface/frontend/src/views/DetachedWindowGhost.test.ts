// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appRecord, windowRecord } from "../testing/records";
import { DetachedWindowGhost } from "./DetachedWindowGhost";
import type { DetachedWindowGhostAttrs } from "./DetachedWindowGhost";

afterEach(unmountViews);

function render(overrides: Partial<DetachedWindowGhostAttrs> = {}): HTMLElement {
  const attrs: DetachedWindowGhostAttrs = {
    window: windowRecord("win-1", "docs", "/?doc=1", { title: "Plan" }),
    app: appRecord("docs"),
    title: "Plan",
    rect: { x: 50, y: 60, width: 640, height: 480 },
    stackIndex: 1,
    onShow: vi.fn(),
    onBringBack: vi.fn(),
    ...overrides,
  };
  const root = mountView(() => m(DetachedWindowGhost, attrs));
  return root.querySelector('[data-detached-window="win-1"]') as HTMLElement;
}

describe("DetachedWindowGhost", () => {
  it("stands at the window's rectangle in the window's own slot of the stacking order, named by its title", () => {
    const element = render();
    expect([element.style.left, element.style.top, element.style.width, element.style.height]).toEqual([
      "50px",
      "60px",
      "640px",
      "480px",
    ]);
    expect(element.style.zIndex).toBe("4");
    expect(element.querySelector(".window-title")?.textContent).toBe("Plan");
    // No page and no chrome: the window's page lives in the chrome's own desktop window.
    expect(element.querySelector("[data-window-content]")).toBeNull();
    expect(element.querySelector("[data-drag-handle]")).toBeNull();
  });

  it("offers Show and Bring back, each reaching its own handler", () => {
    const onShow = vi.fn();
    const onBringBack = vi.fn();
    const element = render({ onShow, onBringBack });
    (element.querySelector('[data-ghost-action="show"]') as HTMLElement).click();
    expect(onShow).toHaveBeenCalledTimes(1);
    expect(onBringBack).not.toHaveBeenCalled();
    (element.querySelector('[data-ghost-action="bring-back"]') as HTMLElement).click();
    expect(onBringBack).toHaveBeenCalledTimes(1);
    expect(onShow).toHaveBeenCalledTimes(1);
  });
});
