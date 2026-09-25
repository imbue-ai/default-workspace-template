// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WINDOW_ZONES, windowSizeRow, windowSizeSubmenuRow } from "./WindowSizeRow";
import type { WindowSizeActions } from "./WindowSizeRow";

afterEach(unmountViews);

function actions(): WindowSizeActions & { setState: ReturnType<typeof vi.fn>; setFrame: ReturnType<typeof vi.fn> } {
  return { setState: vi.fn(), setFrame: vi.fn() };
}

/** The grid as the menu renders it, from either shape of row. */
function render(children: m.Children): HTMLElement {
  const root = mountView(() => m("div", children));
  return root;
}

function tile(root: HTMLElement, key: string): HTMLElement {
  const found = root.querySelector<HTMLElement>(`[data-window-zone="${key}"]`);
  if (found === null) throw new Error(`no ${key} tile`);
  return found;
}

describe("the zone grid", () => {
  it("offers the whole backdrop, the four halves and the four quarters, each named", () => {
    expect(WINDOW_ZONES.map((zone) => zone.key)).toEqual([
      "full",
      "left-half",
      "right-half",
      "top-half",
      "bottom-half",
      "top-left-quarter",
      "top-right-quarter",
      "bottom-left-quarter",
      "bottom-right-quarter",
    ]);
    for (const zone of WINDOW_ZONES) {
      expect(zone.label).not.toBe("");
    }
  });

  it("covers the backdrop with the halves and with the quarters, and nothing over its edge", () => {
    for (const zone of WINDOW_ZONES) {
      expect(zone.frame.x + zone.frame.width).toBeLessThanOrEqual(1);
      expect(zone.frame.y + zone.frame.height).toBeLessThanOrEqual(1);
    }
    const area = (keys: string[]): number =>
      WINDOW_ZONES.filter((zone) => keys.includes(zone.key)).reduce(
        (total, zone) => total + zone.frame.width * zone.frame.height,
        0,
      );
    expect(area(["left-half", "right-half"])).toBe(1);
    expect(area(["top-half", "bottom-half"])).toBe(1);
    expect(area(["top-left-quarter", "top-right-quarter", "bottom-left-quarter", "bottom-right-quarter"])).toBe(1);
  });

  it("sets the state for a zone a window state stands for, and the frame for the rest", () => {
    const placed = actions();
    const onPlaced = vi.fn();
    const root = render(windowSizeRow(placed, onPlaced).render());

    tile(root, "left-half").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(placed.setState).toHaveBeenCalledWith("SNAPPED_LEFT");
    expect(placed.setFrame).not.toHaveBeenCalled();

    tile(root, "bottom-right-quarter").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(placed.setFrame).toHaveBeenCalledWith({ x: 0.5, y: 0.5, width: 0.5, height: 0.5 });
    expect(onPlaced).toHaveBeenCalledTimes(2);
  });

  it("draws each pictogram from the very frame its tile places the window at", () => {
    const root = render(windowSizeRow(actions(), vi.fn()).render());
    // The glyph's outer square is the backdrop; the filled one is the zone, at 16 units across.
    const filled = tile(root, "right-half").querySelectorAll("rect")[1];
    expect(filled.getAttribute("x")).toBe("12");
    expect(filled.getAttribute("y")).toBe("4");
    expect(filled.getAttribute("width")).toBe("8");
    expect(filled.getAttribute("height")).toBe("16");
  });

  it("carries its own heading in the menu the maximize control opens", () => {
    const row = windowSizeRow(actions(), vi.fn());
    expect(row.kind).toBe("custom");
    expect(render(row.render()).textContent).toContain("Move and resize");
  });

  it("is named by the row instead in the window menu, which opens it as a submenu", () => {
    const row = windowSizeSubmenuRow(actions(), vi.fn());
    expect(row.kind).toBe("submenu");
    expect(row.label).toBe("Move and resize");
    expect(row.rows).toBeUndefined();
    const content = render(row.content?.() ?? null);
    expect(content.textContent).not.toContain("Move and resize");
    expect(content.querySelectorAll("[data-window-zone]")).toHaveLength(WINDOW_ZONES.length);
  });

  it("stops a tile's click short of the row under it, which would close the menu on its own terms", () => {
    const root = render(windowSizeRow(actions(), vi.fn()).render());
    const reachedTheRow = vi.fn();
    root.addEventListener("click", reachedTheRow);
    tile(root, "full").dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(reachedTheRow).not.toHaveBeenCalled();
  });
});
