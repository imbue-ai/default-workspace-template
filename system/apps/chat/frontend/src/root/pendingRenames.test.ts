import { beforeEach, describe, expect, it } from "vitest";

import {
  acceptPendingRename,
  dropPendingRename,
  settleAcceptedRenames,
  showPendingRename,
  titleWithPendingRename,
  withPendingTitles,
} from "./pendingRenames";
import type { ChatRow } from "./rows";

function row(chatId: string, title: string): ChatRow {
  return {
    chatId,
    title,
    status: "idle",
    labels: {},
    agentIds: [chatId],
    lastActiveMs: null,
    isProvisional: false,
    isMain: false,
  };
}

const titles = (rows: readonly ChatRow[]): string[] => rows.map((entry) => entry.title);

describe("pending renames", () => {
  beforeEach(() => {
    // Each test leaves its rename settled, so the next starts from the chat app's own names.
    for (const chatId of ["chat-a", "chat-b"]) {
      acceptPendingRename(chatId, "Plans");
      acceptPendingRename(chatId, "Trip");
    }
    settleAcceptedRenames();
  });

  it("shows the typed name at once, and only on the renamed chat", () => {
    showPendingRename("chat-a", "Plans");

    expect(titles(withPendingTitles([row("chat-a", "Chat 1"), row("chat-b", "Chat 2")]))).toEqual(["Plans", "Chat 2"]);
    expect(titleWithPendingRename("chat-a", "Chat 1")).toBe("Plans");
  });

  it("keeps the typed name through chat lists that still carry the old one, until the rename is accepted", () => {
    showPendingRename("chat-a", "Plans");
    settleAcceptedRenames();

    expect(titles(withPendingTitles([row("chat-a", "Chat 1")]))).toEqual(["Plans"]);

    acceptPendingRename("chat-a", "Plans");
    settleAcceptedRenames();

    expect(titles(withPendingTitles([row("chat-a", "Chat 1")]))).toEqual(["Chat 1"]);
  });

  it("keeps the typed name over an automatic name the chat app shows before it answers", () => {
    showPendingRename("chat-a", "Plans");

    expect(titles(withPendingTitles([row("chat-a", "Trip planning: a week in Lisbon")]))).toEqual(["Plans"]);
    expect(titleWithPendingRename("chat-a", "Trip planning: a week in Lisbon")).toBe("Plans");
  });

  it("brings the old name back when the rename is refused", () => {
    showPendingRename("chat-a", "Plans");

    dropPendingRename("chat-a", "Plans");

    expect(titles(withPendingTitles([row("chat-a", "Chat 1")]))).toEqual(["Chat 1"]);
  });

  it("keeps a second rename typed before the first was answered", () => {
    showPendingRename("chat-a", "Plans");
    showPendingRename("chat-a", "Trip");

    // The first refusal arrives late: the second rename's name stays.
    dropPendingRename("chat-a", "Plans");

    expect(titles(withPendingTitles([row("chat-a", "Chat 1")]))).toEqual(["Trip"]);
  });
});
