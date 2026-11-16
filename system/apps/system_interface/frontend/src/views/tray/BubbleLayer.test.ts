// @vitest-environment jsdom
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import { afterEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import type { Bubble } from "../../model/Bubbles";
import { rosterParticipantRecord } from "../../testing/records";
import { BubbleLayer } from "./BubbleLayer";
import type { BubbleLayerAttrs } from "./BubbleLayer";

const bob = rosterParticipantRecord("user:user-bob-4471", { display_name: "Bob" });

function bubble(speaker: string, lines: readonly string[], overrides: Partial<Bubble> = {}): Bubble {
  return {
    speaker,
    chatId: "agent-2",
    chatTitle: "Lounge",
    lines,
    foldedCount: 0,
    messageId: `m-${lines[lines.length - 1]}`,
    isMention: false,
    changedAt: 1000,
    ...overrides,
  };
}

function render(overrides: Partial<BubbleLayerAttrs> = {}): HTMLElement {
  const attrs: BubbleLayerAttrs = {
    bubbles: [bubble(bob.key, ["hi all"]), bubble("workspace-agent", ["a", "b", "c"], { foldedCount: 2 })],
    anchorOf: () => null,
    rosterEntry: (key) => (key === bob.key ? bob : null),
    onOpen: vi.fn(),
    onDismiss: vi.fn(),
    ...overrides,
  };
  return mountView(() => m(BubbleLayer, attrs));
}

afterEach(() => {
  unmountViews();
});

describe("BubbleLayer", () => {
  it("draws one bubble per speaker, named from the roster or by key, with its lines and the folded count", () => {
    const root = render();
    const drawn = Array.from(root.querySelectorAll<HTMLElement>("[data-bubble]"));
    expect(drawn.map((element) => element.getAttribute("data-bubble"))).toEqual([bob.key, "workspace-agent"]);
    expect(drawn[0].textContent).toContain("Bob in Lounge");
    expect(drawn[0].querySelectorAll("[data-bubble-line]")).toHaveLength(1);
    expect(drawn[1].textContent).toContain("workspace-agent in Lounge");
    expect(Array.from(drawn[1].querySelectorAll("[data-bubble-line]")).map((line) => line.textContent)).toEqual([
      "a",
      "b",
      "c",
    ]);
    expect(drawn[1].textContent).toContain("and 2 more");
    expect(drawn.map((element) => element.getAttribute("data-bubble-mention"))).toEqual([null, null]);
  });

  it("marks a mention of the viewer, opens the chat on a click, and dismisses from the X without opening", () => {
    const onOpen = vi.fn();
    const onDismiss = vi.fn();
    const root = render({
      bubbles: [bubble(bob.key, ["@Josh look"], { isMention: true })],
      onOpen,
      onDismiss,
    });
    const drawn = root.querySelector<HTMLElement>("[data-bubble]") as HTMLElement;
    expect(drawn.getAttribute("data-bubble-mention")).toBe("true");
    (drawn.querySelector("[data-bubble-dismiss]") as HTMLElement).click();
    expect(onDismiss).toHaveBeenCalledWith(bob.key);
    expect(onOpen).not.toHaveBeenCalled();
    drawn.click();
    expect(onOpen).toHaveBeenCalledWith(bob.key);
  });

  it("hangs each bubble over its speaker's entry, higher for each further one, and draws nothing with none up", () => {
    const anchor = { top: 700, right: 900 } as DOMRect;
    const root = render({ anchorOf: (speaker) => (speaker === bob.key ? anchor : null) });
    const [bobs, agents] = Array.from(root.querySelectorAll<HTMLElement>("[data-bubble]"));
    expect(bobs.style.right).toBe(`${window.innerWidth - 900 - 4}px`);
    expect(bobs.style.bottom).toBe(`${window.innerHeight - 700 + 10}px`);
    expect(agents.style.right).toBe("16px");
    expect(agents.style.bottom).toBe(`calc(var(--desk-taskbar-height) + ${10 + 8}px)`);
    unmountViews();
    expect(render({ bubbles: [] }).querySelector("[data-bubble-layer]")).toBeNull();
  });
});
