// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  UNCONFIRMED_AFTER_MS,
  addOutgoing,
  noteBackendArrivals,
  noteOutgoingAccepted,
} from "../models/OutgoingMessages";
import { renderOutgoingMessages } from "./OutgoingMessageView";
import { USER_BUBBLE_CLASS } from "./user-message-display";

describe("the optimistic outgoing bubble", () => {
  it("shows the sent text as a delivered user bubble at once: solid, with no caption", () => {
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    addOutgoing(chatId, "hello there");
    const root = document.createElement("div");

    m.render(root, renderOutgoingMessages(chatId));

    const row = root.querySelector(".outgoing-message");
    expect(row?.getAttribute("class")).not.toMatch(/opacity-/);
    expect(row?.querySelector(".message-user-bubble")?.getAttribute("class")).toBe(USER_BUBBLE_CLASS);
    expect(row?.textContent).toBe("hello there");
  });

  it("keeps the committed user row's spacing, so consecutive sends stand apart", () => {
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    addOutgoing(chatId, "first");
    addOutgoing(chatId, "second");
    const root = document.createElement("div");

    m.render(root, renderOutgoingMessages(chatId));

    const rows = Array.from(root.querySelectorAll(".outgoing-message"));
    expect(rows).toHaveLength(2);
    expect(rows.every((row) => row.classList.contains("mb-5"))).toBe(true);
  });
});

describe("a sent message nothing confirmed", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function rendered(chatId: string): HTMLElement {
    const root = document.createElement("div");
    m.render(root, renderOutgoingMessages(chatId));
    return root;
  }

  it("says it may not have been delivered once its accepted send's arrival is overdue", () => {
    vi.useFakeTimers();
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    const id = addOutgoing(chatId, "hello");
    noteOutgoingAccepted(chatId, id);

    vi.advanceTimersByTime(UNCONFIRMED_AFTER_MS - 1);
    expect(rendered(chatId).querySelector(".outgoing-message-unconfirmed")).toBeNull();

    vi.advanceTimersByTime(1);
    expect(rendered(chatId).querySelector(".outgoing-message-unconfirmed")?.textContent).toBe(
      "This message may not have been delivered.",
    );
  });

  it("never marks a bubble whose arrival came, nor one whose send is still waiting on the backend", () => {
    vi.useFakeTimers();
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    const arrived = addOutgoing(chatId, "arrives");
    noteOutgoingAccepted(chatId, arrived);
    noteBackendArrivals(chatId, ["evt-1"]);
    addOutgoing(chatId, "still sending");

    vi.advanceTimersByTime(UNCONFIRMED_AFTER_MS * 10);

    const root = rendered(chatId);
    expect(root.textContent).toBe("still sending");
    expect(root.querySelector(".outgoing-message-unconfirmed")).toBeNull();
  });
});
