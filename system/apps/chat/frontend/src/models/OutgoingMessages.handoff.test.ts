import { beforeEach, describe, expect, it, vi } from "vitest";

// The bubbles' bookkeeping while a chat switches harness: the chats listener the tracker
// installs is captured here and driven with snapshots by hand.
vi.mock("mithril", () => ({ default: { redraw: vi.fn() } }));
const listeners = vi.hoisted(() => [] as ((chats: unknown[]) => void)[]);
vi.mock("./Chats", () => ({
  addChatsUpdatedListener: (listener: (chats: unknown[]) => void) => listeners.push(listener),
}));

import { chatSnapshotFixture, handoffStateFixture } from "./chatSnapshotFixture";
import {
  addOutgoing,
  dropOutgoingByMessageId,
  getOutgoingMessages,
  noteBackendArrivals,
  trackBackendArrivals,
} from "./OutgoingMessages";

function push(chats: unknown[]): void {
  for (const listener of listeners) listener(chats);
}

describe("the bubbles of a chat switching harness", () => {
  beforeEach(() => {
    listeners.length = 0;
    trackBackendArrivals();
  });

  it("drops a bubble by its send-time id, leaving bubbles without one alone", () => {
    const chat = `a-${Math.random()}`;
    addOutgoing(chat, "no id");
    addOutgoing(chat, "held one", "m-1");
    dropOutgoingByMessageId(chat, ["m-1", "m-unknown"]);
    expect(getOutgoingMessages(chat).map((o) => o.content)).toEqual(["no id"]);
  });

  it("stands a bubble down once the snapshot holds its send, and brings the held sends back when the switch ends", () => {
    const chat = `a-${Math.random()}`;
    addOutgoing(chat, "Carry on in Codex", "trigger-1");
    const converging = chatSnapshotFixture(chat, {
      active_agent: { agent_id: "agent-old" },
      handoff: handoffStateFixture({
        held_sends: [
          { message_id: "trigger-1", text: "Carry on in Codex" },
          { message_id: "m-2", text: "and this" },
        ],
      }),
    });
    push([converging]);
    expect(getOutgoingMessages(chat)).toEqual([]);

    // The successor is created and named while the switch still delivers the held sends.
    push([{ ...converging, active_agent: { ...converging.active_agent, agent_id: "agent-new" } }]);
    // The switch ended on the new agent: both held messages come back as bubbles, in order,
    // carrying their ids, until their turns arrive in the new agent's transcript.
    push([chatSnapshotFixture(chat, { active_agent: { agent_id: "agent-new" } })]);
    expect(getOutgoingMessages(chat).map((o) => [o.content, o.messageId])).toEqual([
      ["Carry on in Codex", "trigger-1"],
      ["and this", "m-2"],
    ]);
    noteBackendArrivals(chat, ["u-1"]);
    expect(getOutgoingMessages(chat).map((o) => o.content)).toEqual(["and this"]);
  });

  it("skips the confirming message when the switch was cancelled, since it returns to the composer", () => {
    const chat = `a-${Math.random()}`;
    const converging = chatSnapshotFixture(chat, {
      active_agent: { agent_id: "agent-old" },
      handoff: handoffStateFixture({
        held_sends: [
          { message_id: "trigger-1", text: "Carry on in Codex" },
          { message_id: "m-2", text: "and this" },
        ],
      }),
    });
    push([converging]);
    push([chatSnapshotFixture(chat, { active_agent: { agent_id: "agent-old" } })]);
    expect(getOutgoingMessages(chat).map((o) => o.content)).toEqual(["and this"]);
  });

  it("does nothing for a chat that ends a push without ever having switched", () => {
    const chat = `a-${Math.random()}`;
    push([chatSnapshotFixture(chat)]);
    expect(getOutgoingMessages(chat)).toEqual([]);
  });
});
