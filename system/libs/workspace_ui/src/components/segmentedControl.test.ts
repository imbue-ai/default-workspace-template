// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import m from "mithril";
import { SegmentedControl, type Segment } from "./segmentedControl";

type Mode = "icons" | "list" | "columns";

const SEGMENTS: readonly Segment<Mode>[] = [
  { value: "icons", label: "View as Icons", icon: "layout-grid", attrs: { "data-view-mode": "icons" } },
  { value: "list", label: "View as List", icon: "list" },
  { value: "columns", label: "View as Columns", icon: "columns" },
];

function render(value: Mode, onSelect: (value: Mode) => void, segments = SEGMENTS): HTMLElement {
  const root = document.createElement("div");
  m.render(root, m(SegmentedControl<Mode>, { label: "View", segments, value, onSelect }));
  return root;
}

function segmentsOf(root: HTMLElement): HTMLButtonElement[] {
  return Array.from(root.querySelectorAll<HTMLButtonElement>('[role="radio"]'));
}

describe("SegmentedControl", () => {
  it("is a named radio group whose chosen segment alone is checked and the tab stop", () => {
    const root = render("list", () => {});
    expect(root.querySelector('[role="radiogroup"]')?.getAttribute("aria-label")).toBe("View");
    const buttons = segmentsOf(root);
    expect(buttons.map((button) => button.getAttribute("aria-checked"))).toEqual(["false", "true", "false"]);
    expect(buttons.map((button) => button.tabIndex)).toEqual([-1, 0, -1]);
  });

  it("names a glyph segment by its label, and carries the attributes it is given", () => {
    const root = render("icons", () => {});
    const first = segmentsOf(root)[0];
    expect(first.getAttribute("aria-label")).toBe("View as Icons");
    expect(first.querySelector("svg")).not.toBeNull();
    expect(first.getAttribute("data-view-mode")).toBe("icons");
  });

  it("shows a segment without a glyph as its label", () => {
    const root = render("list", () => {}, [
      { value: "icons", label: "Icons" },
      { value: "list", label: "List" },
    ] as Segment<Mode>[]);
    expect(segmentsOf(root).map((button) => button.textContent)).toEqual(["Icons", "List"]);
  });

  it("chooses a segment on a click, and does nothing for the one already chosen", () => {
    const onSelect = vi.fn();
    const root = render("icons", onSelect);
    segmentsOf(root)[0].click();
    expect(onSelect).not.toHaveBeenCalled();
    segmentsOf(root)[2].click();
    expect(onSelect).toHaveBeenCalledWith("columns");
  });

  it("moves the choice along with the arrow keys, wrapping at the ends", () => {
    const onSelect = vi.fn();
    const root = render("icons", onSelect);
    const chosen = segmentsOf(root)[0];
    chosen.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(onSelect).toHaveBeenLastCalledWith("list");
    chosen.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    expect(onSelect).toHaveBeenLastCalledWith("columns");
  });
});
