/** A whole chat-list row for tests: an idle, listed chat that is not the default, with every field overridable. */

import type { ChatRow } from "./rows";

export function chatRowFixture(chatId: string, overrides: Partial<ChatRow> = {}): ChatRow {
  return {
    chatId,
    title: chatId,
    status: "idle",
    labels: {},
    agentIds: [chatId],
    lastActiveMs: null,
    isProvisional: false,
    isDefault: false,
    ...overrides,
  };
}
