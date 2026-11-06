// @vitest-environment jsdom
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import {
  CARD_HOVER_DELAY_MS,
  CARD_LEAVE_GRACE_MS,
  CARD_WIDTH_PX,
  TrayCardFrame,
  createTrayCardHover,
} from "./TrayCard";

const BOB = { kind: "person", key: "user:user-bob-4471" } as const;
const AGENT = { kind: "agent" } as const;

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  unmountViews();
  vi.useRealTimers();
});

describe("createTrayCardHover", () => {
  it("opens an entry's card after the hover delay, anchored on the entry, telling the caller, and not when the pointer leaves first", () => {
    const onChange = vi.fn();
    const hover = createTrayCardHover(onChange);
    const entry = document.createElement("button");
    hover.enterEntry(BOB, entry);
    vi.advanceTimersByTime(CARD_HOVER_DELAY_MS - 1);
    expect(hover.current()).toBeNull();
    vi.advanceTimersByTime(1);
    expect(hover.current()).toEqual(BOB);
    expect(hover.anchor()).toEqual(entry.getBoundingClientRect());
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(BOB);

    hover.close();
    expect(hover.current()).toBeNull();
    expect(onChange).toHaveBeenLastCalledWith(null);
    hover.enterEntry(BOB, entry);
    hover.leaveEntry();
    vi.advanceTimersByTime(CARD_HOVER_DELAY_MS);
    expect(hover.current()).toBeNull();
  });

  it("keeps the card while the pointer reaches it, closes after the grace otherwise, and switches entries at once", () => {
    const hover = createTrayCardHover(vi.fn());
    const entry = document.createElement("button");
    hover.enterEntry(BOB, entry);
    vi.advanceTimersByTime(CARD_HOVER_DELAY_MS);

    hover.leaveEntry();
    vi.advanceTimersByTime(CARD_LEAVE_GRACE_MS - 1);
    hover.enterCard();
    vi.advanceTimersByTime(CARD_LEAVE_GRACE_MS);
    expect(hover.current()).toEqual(BOB);

    hover.leaveCard();
    vi.advanceTimersByTime(CARD_LEAVE_GRACE_MS - 1);
    expect(hover.current()).toEqual(BOB);
    vi.advanceTimersByTime(1);
    expect(hover.current()).toBeNull();
    expect(hover.anchor()).toBeNull();

    hover.enterEntry(BOB, entry);
    vi.advanceTimersByTime(CARD_HOVER_DELAY_MS);
    hover.leaveEntry();
    const agentEntry = document.createElement("button");
    hover.enterEntry(AGENT, agentEntry);
    expect(hover.current()).toEqual(AGENT);
    vi.advanceTimersByTime(CARD_LEAVE_GRACE_MS);
    expect(hover.current()).toEqual(AGENT);

    hover.leaveEntry();
    hover.dispose();
    vi.advanceTimersByTime(CARD_LEAVE_GRACE_MS);
    expect(hover.current()).toEqual(AGENT);
  });
});

describe("TrayCardFrame", () => {
  it("is a labelled dialog of the card's width, hanging above its anchor with right edges aligned", () => {
    const onEnter = vi.fn();
    const onLeave = vi.fn();
    const anchor = { top: 700, right: 900 } as DOMRect;
    const root = mountView(() =>
      m(TrayCardFrame, { anchor, label: "Bob", onEnter, onLeave }, m("span", { "data-card-body": "" }, "hello")),
    );
    const card = root.querySelector<HTMLElement>("[data-tray-card]") as HTMLElement;
    expect(card.getAttribute("role")).toBe("dialog");
    expect(card.getAttribute("aria-label")).toBe("Bob");
    expect(card.style.width).toBe(`${CARD_WIDTH_PX}px`);
    expect(card.style.right).toBe(`${window.innerWidth - 900}px`);
    expect(card.style.bottom).toBe(`${window.innerHeight - 700 + 6}px`);
    expect(card.querySelector("[data-card-body]")?.textContent).toBe("hello");
    card.dispatchEvent(new MouseEvent("mouseenter"));
    card.dispatchEvent(new MouseEvent("mouseleave"));
    expect(onEnter).toHaveBeenCalledTimes(1);
    expect(onLeave).toHaveBeenCalledTimes(1);
  });
});
