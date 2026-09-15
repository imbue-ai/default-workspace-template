/** A whole chat snapshot for tests, as the chat app pushes one, with every field overridable. */

import type { ActiveAgent, ChatSnapshot } from "./Chats";

export function chatSnapshotFixture(
  chatId: string,
  overrides: Partial<Omit<ChatSnapshot, "active_agent">> & { active_agent?: Partial<ActiveAgent> } = {},
): ChatSnapshot {
  const { active_agent: agentOverrides, ...chatOverrides } = overrides;
  return {
    chat_id: chatId,
    title: chatId,
    name: chatId,
    project: null,
    status: "idle",
    labels: {},
    agent_ids: [chatId],
    handoff: null,
    ...chatOverrides,
    active_agent: {
      agent_id: chatId,
      name: chatId,
      harness: "claude",
      account_id: null,
      state: "RUNNING",
      activity_state: null,
      model_choice: null,
      queued_messages: [],
      shoulder_tap_available: false,
      ...agentOverrides,
    },
  };
}
