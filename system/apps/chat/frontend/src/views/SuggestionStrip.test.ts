// @vitest-environment jsdom
import m from "mithril";
import { describe, expect, it, vi } from "vitest";
import type { Suggestion } from "../models/Suggestions";
import {
  emptyNextStepLine,
  suggestionLine,
  suggestionPills,
  SuggestionStrip,
  type SuggestionStripAttrs,
  suggestionsOffNotice,
} from "./SuggestionStrip";

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
    count: START.length,
    isOpen: true,
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

function lineToggle(root: HTMLElement): HTMLButtonElement | null {
  return root.querySelector<HTMLButtonElement>(".suggestion-line-toggle");
}

/** A new-chat strip as the page draws one: its pills always shown, with nothing to show or hide them. */
function renderNewChatStrip(overrides: Partial<SuggestionStripAttrs> = {}): {
  root: HTMLElement;
  attrs: SuggestionStripAttrs;
} {
  return renderStrip({ placement: "new_chat", onToggle: null, ...overrides });
}

describe("the pills of a set", () => {
  it("are a list on the user's side, one to a row and all as wide as the widest, set in from the edge", () => {
    const root = document.createElement("div");
    m.render(root, suggestionPills(START, vi.fn()));
    const list = root.querySelector<HTMLElement>(".suggestion-strip-pills");

    // A column whose items stretch is one pill to a row, each the group's width; the group is as wide as its
    // widest pill, never wider than the column, pushed to the right to end at its edge.
    expect([...list!.classList]).toEqual(
      expect.arrayContaining(["flex", "flex-col", "items-stretch", "w-fit", "max-w-full", "ml-auto"]),
    );
    expect(list!.classList.contains("flex-wrap")).toBe(false);
    expect([...list!.children].map((child) => child.tagName)).toEqual(["BUTTON", "BUTTON", "BUTTON"]);
  });

  it("draws each suggestion as a pill, in order, and a click picks that suggestion", () => {
    const onPick = vi.fn();
    const root = document.createElement("div");
    m.render(root, suggestionPills(START, onPick));

    expect(pills(root).map((pill) => pill.textContent)).toEqual(START.map((suggestion) => suggestion.label));

    pills(root)[2].click();
    expect(onPick).toHaveBeenCalledExactlyOnceWith(START[2]);
  });

  it("draws every suggestion of a long set", () => {
    const long: Suggestion[] = ["One", "Two", "Three", "Four", "Five"].map((label) => ({
      suggestion_id: label.toLowerCase(),
      label,
      is_recommended: false,
    }));
    const root = document.createElement("div");
    m.render(root, suggestionPills(long, vi.fn()));

    expect(pills(root).map((pill) => pill.textContent)).toEqual(["One", "Two", "Three", "Four", "Five"]);
  });

  it("marks the recommended suggestion alone for the green look", () => {
    const root = document.createElement("div");
    m.render(
      root,
      suggestionPills(
        [
          { suggestion_id: "go", label: "Go with your recommendations", is_recommended: true },
          { suggestion_id: "wait", label: "Let me think about it", is_recommended: false },
        ],
        vi.fn(),
      ),
    );

    expect(pills(root).map((pill) => pill.classList.contains("suggestion-pill--recommended"))).toEqual([true, false]);
  });
});

describe("the new-chat strip", () => {
  it("is, in the footer, a quiet line that holds the link that switches the placement off", () => {
    const { root, attrs } = renderNewChatStrip();
    const strip = root.querySelector<HTMLElement>(".suggestion-strip");
    const line = root.querySelector<HTMLElement>(".suggestion-line");
    const link = root.querySelector<HTMLButtonElement>(".suggestion-strip-disable");

    expect(strip?.classList.contains("suggestion-strip--open")).toBe(true);
    // The pills are drawn elsewhere, over the transcript: the line is all of the strip the footer holds, and
    // nothing of it rises in.
    expect([...strip!.children]).toEqual([line]);
    expect(pills(root)).toEqual([]);
    expect(root.querySelector(".suggestion-rise")).toBeNull();
    // The line is a reply's line in height and look, and holds the link alone, at its right end.
    expect([...line!.classList]).toEqual(expect.arrayContaining(["h-5", "justify-end"]));
    expect([...line!.children]).toEqual([link]);
    expect(link?.textContent).toBe("Disable new chat suggestions");
    expect(lineToggle(root)).toBeNull();

    link!.click();
    expect(attrs.onDisable).toHaveBeenCalledTimes(1);
  });

  it("offers no such link to a viewer who may not switch it off, and keeps the line's height", () => {
    const { root } = renderNewChatStrip({ onDisable: null });

    expect(root.querySelector(".suggestion-strip-disable")).toBeNull();
    expect(root.querySelectorAll("button")).toHaveLength(0);
    expect(root.querySelector(".suggestion-line")?.classList.contains("h-5")).toBe(true);
  });
});

describe("the next-step strip", () => {
  it("is, with its pills hidden, one quiet line saying how many suggestions wait", () => {
    const { root, attrs } = renderStrip({ placement: "next_step", isOpen: false });
    const toggle = lineToggle(root);

    expect(toggle?.textContent).toBe("See 3 suggestions");
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    // The quiet look of a strip's disable link, at the line's right end.
    expect([...toggle!.classList]).toEqual(
      expect.arrayContaining(["text-(length:--font-size-helper)", "text-faint", "hover:underline"]),
    );
    expect(toggle!.parentElement?.classList.contains("justify-end")).toBe(true);
    expect([...toggle!.parentElement!.children]).toEqual([toggle]);
    expect(pills(root)).toEqual([]);
    expect(root.querySelector(".suggestion-strip-disable")).toBeNull();
    expect(root.querySelector(".suggestion-strip--open")).toBeNull();

    toggle!.click();
    expect(attrs.onToggle).toHaveBeenCalledTimes(1);
  });

  it("counts one suggestion in the singular", () => {
    const { root } = renderStrip({ placement: "next_step", isOpen: false, count: 1 });

    expect(lineToggle(root)?.textContent).toBe("See 1 suggestion");
  });

  it("offers, with its pills shown, to hide them and, just left of that, to switch them off", () => {
    const { root, attrs } = renderStrip({ placement: "next_step" });
    const toggle = lineToggle(root);
    const link = root.querySelector<HTMLButtonElement>(".suggestion-strip-disable");

    expect(root.querySelector(".suggestion-strip--open")).not.toBeNull();
    expect(toggle?.textContent).toBe("Hide suggestions");
    expect(toggle?.getAttribute("aria-expanded")).toBe("true");
    expect(link?.textContent).toBe("Disable suggestions");
    // Both sit at the line's right end, the link immediately left of the button, 16px apart.
    const line = toggle!.parentElement!;
    expect([...line.children]).toEqual([link, toggle]);
    expect([...line.classList]).toEqual(expect.arrayContaining(["flex", "justify-end", "gap-4"]));
    expect(link!.classList.contains("ml-auto") || toggle!.classList.contains("ml-auto")).toBe(false);

    link!.click();
    expect(attrs.onDisable).toHaveBeenCalledTimes(1);
    toggle!.click();
    expect(attrs.onToggle).toHaveBeenCalledTimes(1);
  });

  it("holds the same one line, and nothing else, with its pills shown as with them hidden", () => {
    for (const isOpen of [true, false]) {
      const { root } = renderStrip({ placement: "next_step", isOpen });
      const strip = root.querySelector<HTMLElement>(".suggestion-strip")!;

      expect([...strip.children].map((child) => child.className.split(" ")[0])).toEqual(["suggestion-line"]);
      expect(pills(root)).toEqual([]);
    }
  });

  it("offers the link to switch them off to nobody but a viewer who may", () => {
    const { root } = renderStrip({ placement: "next_step", onDisable: null });

    expect(root.querySelector(".suggestion-strip-disable")).toBeNull();
    expect(lineToggle(root)?.textContent).toBe("Hide suggestions");
  });
});

describe("the line a reply's suggestions wait behind", () => {
  function line(root: HTMLElement): HTMLElement {
    return root.querySelector<HTMLElement>(".suggestion-line")!;
  }

  it("has one fixed height whether it is empty, says how many wait, or offers to hide them", () => {
    const drawn = [
      null,
      { count: 3, isOpen: false, onToggle: vi.fn(), onDisable: vi.fn() },
      { count: 3, isOpen: true, onToggle: vi.fn(), onDisable: vi.fn() },
    ].map((attrs) => {
      const root = document.createElement("div");
      m.render(root, suggestionLine(attrs));
      return line(root);
    });

    for (const element of drawn) expect(element.classList.contains("h-5")).toBe(true);
    expect(drawn.map((element) => element.textContent)).toEqual([
      "",
      "See 3 suggestions",
      "Disable suggestionsHide suggestions",
    ]);
  });

  it("keeps its height above the composer with no set to speak of, holding nothing to click", () => {
    const root = document.createElement("div");
    m.render(root, emptyNextStepLine());

    expect(line(root).classList.contains("h-5")).toBe(true);
    expect(root.querySelectorAll("button")).toHaveLength(0);
    expect(root.firstElementChild?.classList.contains("suggestion-strip-placeholder")).toBe(true);
  });

  it("offers no way to switch the placement off while the suggestions are hidden", () => {
    const root = document.createElement("div");
    m.render(root, suggestionLine({ count: 2, isOpen: false, onToggle: vi.fn(), onDisable: vi.fn() }));

    expect(root.querySelector(".suggestion-strip-disable")).toBeNull();
  });
});

describe("what each placement's strip says", () => {
  it("new_chat's link and its notice say what they do", () => {
    const { root: strip } = renderNewChatStrip();
    expect(strip.querySelector(".suggestion-strip-disable")?.textContent).toBe("Disable new chat suggestions");
    expect(strip.textContent).toBe("Disable new chat suggestions");

    const root = document.createElement("div");
    m.render(root, suggestionsOffNotice("new_chat", vi.fn()));
    expect(root.querySelector(".suggestions-off-notice span")?.textContent).toBe("New chat suggestions are off.");
  });

  it("next_step's line and its notice say what they do", () => {
    const hidden = renderStrip({ placement: "next_step", isOpen: false }).root;
    expect(hidden.textContent).toBe("See 3 suggestions");

    const onUndo = vi.fn();
    const root = document.createElement("div");
    m.render(root, suggestionsOffNotice("next_step", onUndo));
    expect(root.querySelector(".suggestions-off-notice span")?.textContent).toBe("Next step suggestions are off.");
    const undo = root.querySelector<HTMLButtonElement>(".suggestions-off-undo");
    expect(undo?.textContent).toBe("Undo");
    undo!.click();
    expect(onUndo).toHaveBeenCalledTimes(1);
  });
});
