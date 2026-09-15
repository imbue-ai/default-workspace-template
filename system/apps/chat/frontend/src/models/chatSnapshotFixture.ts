/** A whole chat snapshot for tests, as the chat app pushes one, with every field overridable. */

import type { ActiveAgent, ChatSnapshot, HandoffState } from "./Chats";

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

/** A chat's in-progress switch to another harness (the backend's ``HandoffState``), for tests:
 *  a claude chat moving to a codex account, with the confirming message held. */
export function handoffStateFixture(overrides: Partial<HandoffState> = {}): HandoffState {
  return {
    kind: "handoff",
    phase: "summarizing",
    target_lane: "openai",
    target_account_id: "acct-openai",
    target_harness: "codex",
    target_label: "Codex",
    held_sends: [{ message_id: "trigger-1", text: "Carry on in Codex" }],
    error: null,
    ...overrides,
  };
}

/** A chat's in-progress rebind (the same ``HandoffState`` shape), for tests: a claude chat's agent
 *  restarting on a second Anthropic account, with the confirming message held. */
export function rebindStateFixture(overrides: Partial<HandoffState> = {}): HandoffState {
  return {
    kind: "rebind",
    phase: "restarting",
    target_lane: "anthropic",
    target_account_id: "acct-anthropic-2",
    target_harness: "claude",
    target_label: "Anthropic 2 (Claude Code)",
    held_sends: [{ message_id: "trigger-1", text: "Carry on on the other account" }],
    error: null,
    ...overrides,
  };
}
