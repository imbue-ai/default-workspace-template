// @vitest-environment jsdom
import m from "mithril";
import { describe, expect, it } from "vitest";

import { addOutgoing } from "../models/OutgoingMessages";
import { renderOutgoingMessages } from "./OutgoingMessageView";

describe("the optimistic outgoing bubble", () => {
  it("shows the sent text faded, with no caption under it", () => {
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    addOutgoing(chatId, "hello there");
    const root = document.createElement("div");

    m.render(root, renderOutgoingMessages(chatId));

    const row = root.querySelector(".outgoing-message");
    expect(row?.getAttribute("class")).toContain("opacity-60");
    expect(row?.textContent).toBe("hello there");
  });
});
