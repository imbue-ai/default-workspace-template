import { describe, expect, it, vi, beforeEach } from "vitest";
import type { ChatSnapshot, QueuedMessage } from "../models/Chats";

const mocks = vi.hoisted(() => ({
  chat: undefined as ChatSnapshot | undefined,
  carriedMessageIds: new Set<string>(),
  // The agent's turns from users' messages since the switch began, as the page's transcript holds them.
  sentTurns: 0,
}));

vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: () => mocks.chat,
}));
vi.mock("../models/Response", () => ({
  countSentTurnsSince: () => mocks.sentTurns,
  isMessageCarriedBySwitch: (_chatId: string, messageId: string) => mocks.carriedMessageIds.has(messageId),
}));
vi.mock("../models/OutgoingMessages", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/OutgoingMessages")>()),
  getOutgoingMessages: () => [],
}));

import { chatSnapshotFixture, handoffStateFixture, rebindStateFixture } from "../models/chatSnapshotFixture";
import { heldSendsShownElsewhere, renderHeldSends } from "./HeldSendView";

const CHAT_ID = "agent-held";

const HELD_SENDS = [
  { message_id: "trigger-1", text: "Carry on on the other account", is_delivered: true },
  { message_id: "m-2", text: "and this", is_delivered: true },
  { message_id: "m-3", text: "and that", is_delivered: false },
];

function switching(queued: QueuedMessage[] = []): ChatSnapshot {
  return chatSnapshotFixture(CHAT_ID, {
    active_agent: { queued_messages: queued },
    handoff: rebindStateFixture({ phase: "restarting", held_sends: HELD_SENDS }),
  });
}

function renderedKeys(): unknown[] {
  return renderHeldSends(CHAT_ID).map((vnode) => vnode.key);
}

describe("renderHeldSends", () => {
  beforeEach(() => {
    mocks.carriedMessageIds = new Set();
    mocks.sentTurns = 0;
    mocks.chat = switching();
  });

  it("draws every send the switch lists while none of them shows in its real form", () => {
    expect(renderedKeys()).toEqual(["held-trigger-1", "held-m-2", "held-m-3"]);
  });

  it("stands delivered sends down, oldest first, as the agent's turns for them reach the transcript", () => {
    mocks.sentTurns = 1;
    expect(renderedKeys()).toEqual(["held-m-2", "held-m-3"]);
  });

  it("counts a delivered send waiting in the agent's queue as shown there", () => {
    mocks.sentTurns = 1;
    mocks.chat = switching([{ queued_id: "q-2", content: "and this", timestamp: "2026-09-14T12:00:06Z" }]);
    expect(heldSendsShownElsewhere(CHAT_ID)).toBe(2);
    expect(renderedKeys()).toEqual(["held-m-3"]);
  });

  it("never stands down a send the switch still holds, whatever the transcript holds", () => {
    mocks.sentTurns = 4;
    expect(renderedKeys()).toEqual(["held-m-3"]);
  });

  it("leaves a confirming message the switch marker carries out of the sends the turns account for", () => {
    // Only a handoff's marker carries the confirming message, as the successor's opening turn.
    mocks.chat = chatSnapshotFixture(CHAT_ID, {
      handoff: handoffStateFixture({ phase: "switching", held_sends: HELD_SENDS }),
    });
    mocks.carriedMessageIds = new Set(["trigger-1"]);
    mocks.sentTurns = 1;
    expect(renderedKeys()).toEqual(["held-m-3"]);
  });
});
