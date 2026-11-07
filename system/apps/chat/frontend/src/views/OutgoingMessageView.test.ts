// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";

import { addOutgoing } from "../models/OutgoingMessages";
import { setParticipantsForTesting } from "../models/Participants";
import { ownerAndVisitorRosterFixture } from "../models/participantFixture";
import { renderOutgoingMessages } from "./OutgoingMessageView";
import { OTHER_BUBBLE_CLASS, OTHER_BUBBLE_CONTINUED_CLASS, USER_BUBBLE_CLASS } from "./user-message-display";

describe("the optimistic outgoing bubble", () => {
  it("shows the sent text as a delivered user bubble at once: solid, with no caption", () => {
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    addOutgoing(chatId, "hello there");
    const root = document.createElement("div");

    m.render(root, renderOutgoingMessages(chatId));

    const row = root.querySelector(".outgoing-message");
    expect(row?.getAttribute("class")).not.toMatch(/opacity-/);
    expect(row?.querySelector(".message-user-bubble")?.getAttribute("class")).toBe(USER_BUBBLE_CLASS);
    expect(row?.querySelector(".message-user-bubble")?.textContent).toBe("hello there");
    // Named like the turn it becomes, which opens a run of the viewer's own.
    expect(row?.querySelector(".message-you")?.textContent).toBe("You");
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

  it("names the viewer once over a run of their sends, and again past a notice, as the turns they become", () => {
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    addOutgoing(chatId, "first");
    addOutgoing(chatId, "second");
    addOutgoing(
      chatId,
      "<background-task-report>\n<summary>Build (finished)</summary>\nbuilt\n</background-task-report>",
      "m-report",
      { display: "notice", display_label: "Background task", display_body: "Build (finished)" },
    );
    addOutgoing(chatId, "third");
    const root = document.createElement("div");

    // Under the viewer's own turn, the first send carries on its run.
    m.render(root, renderOutgoingMessages(chatId, { isOpen: true, author: null }));

    const named = Array.from(root.querySelectorAll(".outgoing-message")).map(
      (row) => row.querySelector(".message-you") !== null,
    );
    expect(named).toEqual([false, false, true]);
  });
});

describe("a not-yet-delivered message in a shared workspace", () => {
  const roster = ownerAndVisitorRosterFixture();

  afterEach(() => {
    setParticipantsForTesting(null, []);
  });

  it("draws another person's held send under their name, and the viewer's own send as theirs", () => {
    setParticipantsForTesting("user:owner-1", roster);
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    addOutgoing(chatId, "hi", "m-1", undefined, { author: "user:glenn-2", author_name: "glenn" });
    addOutgoing(chatId, "and mine");
    const root = document.createElement("div");

    m.render(root, renderOutgoingMessages(chatId));

    const [glenns, owners] = Array.from(root.querySelectorAll(".outgoing-message"));
    expect(glenns?.classList.contains("message-other")).toBe(true);
    expect(glenns?.querySelector(".message-author")?.textContent).toBe("Glenn");
    expect(glenns?.querySelector(".message-user-bubble")?.getAttribute("class")).toBe(OTHER_BUBBLE_CLASS);
    expect(owners?.classList.contains("message-other")).toBe(false);
    expect(owners?.querySelector(".message-user-bubble")?.getAttribute("class")).toBe(USER_BUBBLE_CLASS);
  });

  it("names another person once over a run of their sends, and the viewer again after it, as their turns will", () => {
    setParticipantsForTesting("user:owner-1", roster);
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    addOutgoing(chatId, "hi", "m-1", undefined, { author: "user:glenn-2", author_name: "glenn" });
    addOutgoing(chatId, "and this", "m-2", undefined, { author: "user:glenn-2", author_name: "glenn" });
    addOutgoing(chatId, "mine");
    const root = document.createElement("div");

    // Under the viewer's own turn, Glenn's first send still opens a run of his.
    m.render(root, renderOutgoingMessages(chatId, { isOpen: true, author: null }));

    const [glennsFirst, glennsSecond, owners] = Array.from(root.querySelectorAll(".outgoing-message"));
    expect(glennsFirst?.querySelector(".message-author")?.textContent).toBe("Glenn");
    expect(glennsFirst?.querySelector(".message-author-picture")).not.toBeNull();
    expect(glennsSecond?.querySelector(".message-author")).toBeNull();
    expect(glennsSecond?.querySelector(".message-author-picture")).toBeNull();
    expect(glennsSecond?.querySelector(".message-user-bubble")?.getAttribute("class")).toBe(
      OTHER_BUBBLE_CONTINUED_CLASS,
    );
    expect(owners?.querySelector(".message-you")?.textContent).toBe("You");
  });

  it("draws the owner's held send under the owner on a visitor's page", () => {
    setParticipantsForTesting("user:glenn-2", roster);
    const chatId = `agent-${Math.random().toString(36).slice(2)}`;
    addOutgoing(chatId, "Carry on in Codex", "trigger-1", undefined, { author: null, author_name: null });
    const root = document.createElement("div");

    m.render(root, renderOutgoingMessages(chatId));

    expect(root.querySelector(".message-author")?.textContent).toBe("Josh");
  });
});
