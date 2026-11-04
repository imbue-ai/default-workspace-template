import { describe, expect, it, vi, beforeEach } from "vitest";
import type { ChatSnapshot } from "../models/Chats";

const mocks = vi.hoisted(() => ({
  chat: undefined as ChatSnapshot | undefined,
  carriedMessageIds: new Set<string>(),
  landed: 0,
}));

vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: () => mocks.chat,
}));
vi.mock("../models/Response", () => ({
  isMessageCarriedBySwitch: (_chatId: string, messageId: string) => mocks.carriedMessageIds.has(messageId),
}));
vi.mock("../models/OutgoingMessages", () => ({
  getOutgoingMessages: () => [],
  heldSendsLandedDuringSwitch: () => mocks.landed,
}));

import { chatSnapshotFixture, handoffStateFixture, rebindStateFixture } from "../models/chatSnapshotFixture";
import { renderHeldSends } from "./HeldSendView";

const CHAT_ID = "agent-held";

const HELD_SENDS = [
  { message_id: "trigger-1", text: "Carry on on the other account" },
  { message_id: "m-2", text: "and this" },
  { message_id: "m-3", text: "and that" },
];

function renderedKeys(): unknown[] {
  return renderHeldSends(CHAT_ID).map((vnode) => vnode.key);
}

describe("renderHeldSends", () => {
  beforeEach(() => {
    mocks.carriedMessageIds = new Set();
    mocks.landed = 0;
    mocks.chat = chatSnapshotFixture(CHAT_ID, {
      handoff: rebindStateFixture({ phase: "switching", held_sends: HELD_SENDS }),
    });
  });

  it("draws every send the switch lists while none of them has landed", () => {
    expect(renderedKeys()).toEqual(["held-trigger-1", "held-m-2", "held-m-3"]);
  });

  it("stands down a delivered send once its turn has landed, oldest first, while the switch goes on", () => {
    mocks.landed = 2;
    expect(renderedKeys()).toEqual(["held-m-3"]);
  });

  it("leaves a confirming message the switch marker carries out of the sends the landed turns account for", () => {
    // Only a handoff's marker carries the confirming message, as the successor's opening turn.
    mocks.chat = chatSnapshotFixture(CHAT_ID, {
      handoff: handoffStateFixture({ phase: "switching", held_sends: HELD_SENDS }),
    });
    mocks.carriedMessageIds = new Set(["trigger-1"]);
    mocks.landed = 1;
    expect(renderedKeys()).toEqual(["held-m-3"]);
  });
});
