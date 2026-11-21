// @vitest-environment jsdom
import m from "mithril";
import { describe, expect, it, vi } from "vitest";
import type { Suggestion, SuggestionPlacement } from "../models/Suggestions";
import { SuggestionStrip, type SuggestionStripAttrs, suggestionsOffNotice } from "./SuggestionStrip";

const START: Suggestion[] = [
  { suggestion_id: "start-describe", label: "Describe what you want to make", is_recommended: false },
  { suggestion_id: "start-ideas", label: "Get ideas for what to make", is_recommended: false },
  { suggestion_id: "start-features", label: "See what Studio can do", is_recommended: false },
];

function renderStrip(overrides: Partial<SuggestionStripAttrs> = {}): {
  root: HTMLElement;
  attrs: SuggestionStripAttrs;
} {
  const attrs: SuggestionStripAttrs = {
    placement: "next_step",
    suggestions: START,
    foldAfter: null,
    isOpen: true,
    onPick: vi.fn(),
    onToggle: vi.fn(),
    onDisable: vi.fn(),
    ...overrides,
  };
  const root = document.createElement("div");
  m.render(root, m(SuggestionStrip, attrs));
  return { root, attrs };
}

function pills(root: HTMLElement): HTMLButtonElement[] {
  return [...root.querySelectorAll<HTMLButtonElement>(".suggestion-pill")];
}

describe("an open strip", () => {
  it("draws each suggestion as a pill, in order, and a click picks that suggestion", () => {
    const { root, attrs } = renderStrip();

    expect(pills(root).map((pill) => pill.textContent)).toEqual(START.map((suggestion) => suggestion.label));
    expect(root.querySelector(".suggestion-strip--open")).not.toBeNull();
    expect(root.querySelector(".suggestion-strip-toggle")).toBeNull();

    pills(root)[2].click();
    expect(attrs.onPick).toHaveBeenCalledTimes(1);
    expect(attrs.onPick).toHaveBeenCalledWith(START[2]);
  });

  it("marks the recommended suggestion alone for the green look", () => {
    const { root } = renderStrip({
      suggestions: [
        { suggestion_id: "go", label: "Go with your recommendations", is_recommended: true },
        { suggestion_id: "wait", label: "Let me think about it", is_recommended: false },
      ],
    });

    expect(pills(root).map((pill) => pill.classList.contains("suggestion-pill--recommended"))).toEqual([true, false]);
  });

  it("offers the link that switches the placement off, under the pills", () => {
    const { root, attrs } = renderStrip();
    const link = root.querySelector<HTMLButtonElement>(".suggestion-strip-disable");

    expect(link?.textContent).toBe("disable next step suggestions");
    expect(pills(root)[0].compareDocumentPosition(link!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    link!.click();
    expect(attrs.onDisable).toHaveBeenCalledTimes(1);
    expect(attrs.onPick).not.toHaveBeenCalled();
  });

  it("offers no such link to a viewer who may not switch it off", () => {
    const { root } = renderStrip({ onDisable: null });

    expect(root.querySelector(".suggestion-strip-disable")).toBeNull();
    expect(pills(root)).toHaveLength(START.length);
  });
});

describe("a strip that folds a long set", () => {
  const LONG: Suggestion[] = ["One", "Two", "Three", "Four", "Five"].map((label) => ({
    suggestion_id: label.toLowerCase(),
    label,
    is_recommended: false,
  }));

  function showMore(root: HTMLElement): HTMLButtonElement | null {
    return root.querySelector<HTMLButtonElement>(".suggestion-strip-more");
  }

  /** Draw the strip again in place, as the page does after a click or a change of what it was handed. */
  function redraw(
    root: HTMLElement,
    attrs: SuggestionStripAttrs,
    overrides: Partial<SuggestionStripAttrs> = {},
  ): void {
    m.render(root, m(SuggestionStrip, { ...attrs, ...overrides }));
  }

  it("draws the first few and a Show more that reveals the rest and then goes", () => {
    const { root, attrs } = renderStrip({ suggestions: LONG, foldAfter: 3 });

    expect(pills(root).map((pill) => pill.textContent)).toEqual(["One", "Two", "Three"]);
    expect(showMore(root)?.textContent).toBe("Show more");

    showMore(root)!.click();
    redraw(root, attrs);

    expect(pills(root).map((pill) => pill.textContent)).toEqual(["One", "Two", "Three", "Four", "Five"]);
    expect(showMore(root)).toBeNull();
    pills(root)[4].click();
    expect(attrs.onPick).toHaveBeenCalledExactlyOnceWith(LONG[4]);
  });

  it("keeps the rest revealed through a collapse to its toggle and back", () => {
    const { root, attrs } = renderStrip({ suggestions: LONG, foldAfter: 3 });
    showMore(root)!.click();

    redraw(root, attrs, { isOpen: false });
    redraw(root, attrs, { isOpen: true });

    expect(pills(root)).toHaveLength(5);
    expect(showMore(root)).toBeNull();
  });

  it("offers no Show more for a set that fits, or for one that is never folded", () => {
    expect(showMore(renderStrip({ suggestions: LONG.slice(0, 3), foldAfter: 3 }).root)).toBeNull();

    const whole = renderStrip({ suggestions: LONG, foldAfter: null });
    expect(pills(whole.root)).toHaveLength(5);
    expect(showMore(whole.root)).toBeNull();
  });
});

describe("a collapsed strip", () => {
  it("is its labelled toggle alone, which a click opens", () => {
    const { root, attrs } = renderStrip({ isOpen: false });
    const toggle = root.querySelector<HTMLButtonElement>(".suggestion-strip-toggle");

    expect(toggle?.textContent).toBe("Next steps");
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    expect(pills(root)).toEqual([]);
    expect(root.querySelector(".suggestion-strip-disable")).toBeNull();
    expect(root.querySelector(".suggestion-strip--open")).toBeNull();

    toggle!.click();
    expect(attrs.onToggle).toHaveBeenCalledTimes(1);
  });
});

describe("what each placement's strip says", () => {
  it.each([
    ["next_step", "Next steps", "disable next step suggestions", "Next step suggestions are off."],
    ["activity", "While you wait", "disable activity suggestions", "Activity suggestions are off."],
    ["new_chat", "Things to try", "disable new chat suggestions", "New chat suggestions are off."],
  ] as const)("%s", (placement: SuggestionPlacement, toggleLabel, disableLink, offNotice) => {
    expect(renderStrip({ placement, isOpen: false }).root.textContent).toBe(toggleLabel);
    expect(renderStrip({ placement }).root.querySelector(".suggestion-strip-disable")?.textContent).toBe(disableLink);

    const onUndo = vi.fn();
    const root = document.createElement("div");
    m.render(root, suggestionsOffNotice(placement, onUndo));
    expect(root.querySelector(".suggestions-off-notice span")?.textContent).toBe(offNotice);
    const undo = root.querySelector<HTMLButtonElement>(".suggestions-off-undo");
    expect(undo?.textContent).toBe("Undo");
    undo!.click();
    expect(onUndo).toHaveBeenCalledTimes(1);
  });
});
