// @vitest-environment jsdom
import m from "mithril";
import { describe, expect, it } from "vitest";

import { buildMessageWithAttachments } from "../models/attachments";
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

  it("draws a sent attachment the way the delivered turn will, without the label it carries to the agent", () => {
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    const path = "/code/uploads/abc/photo.png";
    addOutgoing(chatId, buildMessageWithAttachments("", [path]));
    const root = document.createElement("div");

    m.render(root, renderOutgoingMessages(chatId));

    const bubble = root.querySelector(".message-user-bubble");
    expect(bubble?.textContent).not.toContain("See attachment here:");
    expect(bubble?.querySelector(".markdown-content img")?.getAttribute("src")).toBe(path);
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

  it("draws a message the backend decided is a notice (a held background-task report) as that notice", () => {
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    addOutgoing(
      chatId,
      "<background-task-report>\n<summary>Build (finished)</summary>\nbuilt\n</background-task-report>",
      "m-report",
      { display: "notice", display_label: "Background task", display_body: "Build (finished)" },
    );
    const root = document.createElement("div");

    m.render(root, renderOutgoingMessages(chatId));

    expect(root.querySelector(".message-notice")?.textContent).toBe("Background task: Build (finished)");
    expect(root.querySelector(".message-user-bubble")).toBeNull();
  });
});
