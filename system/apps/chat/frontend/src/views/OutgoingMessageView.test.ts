// @vitest-environment jsdom
import m from "mithril";
import { describe, expect, it } from "vitest";

import { addOutgoing } from "../models/OutgoingMessages";
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
