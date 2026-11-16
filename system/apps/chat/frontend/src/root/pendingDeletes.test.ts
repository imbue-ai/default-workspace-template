import { beforeEach, describe, expect, it } from "vitest";

import { chatRowFixture } from "./chatRowFixture";
import {
  dropPendingDelete,
  hasPendingDelete,
  settlePendingDeletes,
  showPendingDelete,
  withoutPendingDeletes,
} from "./pendingDeletes";

const ROWS = [chatRowFixture("chat-a"), chatRowFixture("chat-b")];

const chatIds = (): string[] => withoutPendingDeletes(ROWS).map((row) => row.chatId);

describe("pending deletes", () => {
  beforeEach(() => {
    settlePendingDeletes(new Set());
  });

  it("takes the chat off the list at once, and only that chat", () => {
    expect(hasPendingDelete()).toBe(false);

    showPendingDelete("chat-a");

    expect(chatIds()).toEqual(["chat-b"]);
    expect(hasPendingDelete()).toBe(true);
  });

  it("keeps the chat off through chat lists that still carry it", () => {
    showPendingDelete("chat-a");

    settlePendingDeletes(new Set(["chat-a", "chat-b"]));

    expect(chatIds()).toEqual(["chat-b"]);
    expect(hasPendingDelete()).toBe(true);
  });

  it("has nothing left to hide once a chat list drops the chat", () => {
    showPendingDelete("chat-a");

    expect(settlePendingDeletes(new Set(["chat-b"]))).toEqual(["chat-a"]);

    expect(hasPendingDelete()).toBe(false);
  });

  it("brings the chat back when the delete is refused", () => {
    showPendingDelete("chat-a");

    dropPendingDelete("chat-a");

    expect(chatIds()).toEqual(["chat-a", "chat-b"]);
    expect(hasPendingDelete()).toBe(false);
  });
});
