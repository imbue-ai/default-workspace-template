import { beforeEach, describe, expect, it, vi } from "vitest";

// The bubbles' bookkeeping while a chat switches harness: the chats listener the tracker
// installs is captured here and driven with snapshots by hand.
vi.mock("mithril", () => ({ default: { redraw: vi.fn() } }));
const listeners = vi.hoisted(() => [] as ((chats: unknown[]) => void)[]);
vi.mock("./Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./Chats")>()),
  addChatsUpdatedListener: (listener: (chats: unknown[]) => void) => listeners.push(listener),
}));

import { chatSnapshotFixture, handoffStateFixture, rebindStateFixture } from "./chatSnapshotFixture";
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

// How many turns users' messages opened for the agent since the switch began, as the page's transcript holds them.
let sentTurnsSinceSwitch = 0;

describe("the bubbles of a chat switching harness", () => {
  beforeEach(() => {
    listeners.length = 0;
    sentTurnsSinceSwitch = 0;
    trackBackendArrivals(
      () => false,
      () => sentTurnsSinceSwitch,
    );
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

  it("brings a held background-task report back with the notice decision the snapshot carried", () => {
    const chat = `a-${Math.random()}`;
    const decision = {
      display: "notice",
      display_label: "Background task",
      display_body: "Build (finished)",
    } as const;
    push([
      chatSnapshotFixture(chat, {
        handoff: rebindStateFixture({
          held_sends: [
            { message_id: "trigger-1", text: "Carry on on the other account" },
            { message_id: "m-report", text: "<background-task-report>...", ...decision },
          ],
        }),
      }),
    ]);
    push([chatSnapshotFixture(chat)]);
    expect(getOutgoingMessages(chat).map((o) => [o.messageId, o.displayDecision])).toEqual([
      ["trigger-1", undefined],
      ["m-report", decision],
    ]);
  });

  it("brings back none of the delivered sends the page already shows, in the transcript or the agent's queue", () => {
    const chat = `a-${Math.random()}`;
    const heldSends = [
      { message_id: "trigger-1", text: "Carry on on the other account", is_delivered: true },
      { message_id: "m-2", text: "and this", is_delivered: true },
    ];
    push([
      chatSnapshotFixture(chat, {
        active_agent: { agent_id: "agent-same" },
        handoff: rebindStateFixture({ held_sends: heldSends }),
      }),
    ]);
    sentTurnsSinceSwitch = 1;
    push([
      chatSnapshotFixture(chat, {
        active_agent: {
          agent_id: "agent-same",
          queued_messages: [{ queued_id: "q-2", content: "and this", timestamp: "2026-09-14T12:00:05Z" }],
        },
      }),
    ]);
    expect(getOutgoingMessages(chat)).toEqual([]);
  });

  it("brings back the delivered sends whose turns have not landed, and every send still held", () => {
    const chat = `a-${Math.random()}`;
    const heldSends = [
      { message_id: "trigger-1", text: "Carry on on the other account", is_delivered: true },
      { message_id: "m-2", text: "and this", is_delivered: true },
      { message_id: "m-3", text: "and that", is_delivered: false },
    ];
    push([
      chatSnapshotFixture(chat, {
        active_agent: { agent_id: "agent-same" },
        handoff: rebindStateFixture({ held_sends: heldSends }),
      }),
    ]);
    // More turns than delivered sends (one from another window, say) never take a send still held.
    sentTurnsSinceSwitch = 3;
    push([chatSnapshotFixture(chat, { active_agent: { agent_id: "agent-same" } })]);
    expect(getOutgoingMessages(chat).map((o) => o.content)).toEqual(["and that"]);

    const other = `a-${Math.random()}`;
    sentTurnsSinceSwitch = 1;
    push([
      chatSnapshotFixture(other, {
        active_agent: { agent_id: "agent-same" },
        handoff: rebindStateFixture({ held_sends: heldSends }),
      }),
    ]);
    push([chatSnapshotFixture(other, { active_agent: { agent_id: "agent-same" } })]);
    expect(getOutgoingMessages(other).map((o) => o.content)).toEqual(["and this", "and that"]);
  });

  it("leaves the confirming message to the switch marker that carries it, and the others to their own turns", () => {
    // The handoff folds the confirming message into the successor's prompt, and the switch
    // marker shows it as the successor's opening turn: it does not come back as a bubble, and
    // the prompt's arrival (not counted: Response.ts leaves it out) consumes nothing.
    listeners.length = 0;
    trackBackendArrivals((_chat, messageId) => messageId === "trigger-1");
    const chat = `a-${Math.random()}`;
    const heldSends = [
      { message_id: "trigger-1", text: "Carry on in Codex" },
      { message_id: "m-2", text: "and this" },
    ];
    push([
      chatSnapshotFixture(chat, {
        active_agent: { agent_id: "agent-old" },
        handoff: handoffStateFixture({ held_sends: heldSends }),
      }),
    ]);
    push([
      chatSnapshotFixture(chat, {
        active_agent: { agent_id: "agent-new" },
        handoff: handoffStateFixture({ phase: "switching", held_sends: heldSends }),
      }),
    ]);
    push([chatSnapshotFixture(chat, { active_agent: { agent_id: "agent-new" } })]);
    expect(getOutgoingMessages(chat).map((o) => [o.content, o.messageId])).toEqual([["and this", "m-2"]]);
    noteBackendArrivals(chat, ["u-2"]);
    expect(getOutgoingMessages(chat)).toEqual([]);
  });

  it("brings every held send back for a page that first saw the switch once the successor was named", () => {
    // A page loaded (or reconnected) while the held sends were being delivered: its first
    // snapshot already names the successor, so the agent alone would read as a cancel.
    const chat = `a-${Math.random()}`;
    push([
      chatSnapshotFixture(chat, {
        active_agent: { agent_id: "agent-new" },
        handoff: handoffStateFixture({
          phase: "switching",
          held_sends: [
            { message_id: "trigger-1", text: "Carry on in Codex" },
            { message_id: "m-2", text: "and this" },
          ],
        }),
      }),
    ]);
    push([chatSnapshotFixture(chat, { active_agent: { agent_id: "agent-new" } })]);
    expect(getOutgoingMessages(chat).map((o) => o.content)).toEqual(["Carry on in Codex", "and this"]);
  });

  // A cancel delivers the others to the agent the chat stays on: they open its next turn only when it is not working.
  it.each([
    { activityState: null, opensTurn: true },
    { activityState: "THINKING", opensTurn: false },
  ])(
    "skips the confirming message when the switch was cancelled, since it returns to the composer (agent $activityState)",
    ({ activityState, opensTurn }) => {
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
      push([chatSnapshotFixture(chat, { active_agent: { agent_id: "agent-old", activity_state: activityState } })]);
      expect(getOutgoingMessages(chat).map((o) => [o.content, o.opensTurn])).toEqual([["and this", opensTurn]]);
    },
  );

  it("leaves the turn to a landed first send, so the next one back waits behind it", () => {
    const chat = `a-${Math.random()}`;
    addOutgoing(chat, "Carry on on the other account", "trigger-1");
    push([
      chatSnapshotFixture(chat, {
        active_agent: { agent_id: "agent-same" },
        handoff: rebindStateFixture({
          held_sends: [
            { message_id: "trigger-1", text: "Carry on on the other account", is_delivered: true },
            { message_id: "m-2", text: "and this", is_delivered: true },
          ],
        }),
      }),
    ]);
    expect(getOutgoingMessages(chat)).toEqual([]);
    noteBackendArrivals(chat, ["u-trigger"]);
    sentTurnsSinceSwitch = 1;
    push([chatSnapshotFixture(chat, { active_agent: { agent_id: "agent-same" } })]);
    expect(getOutgoingMessages(chat).map((o) => [o.content, o.opensTurn])).toEqual([["and this", false]]);
  });

  it("lets the first held send back from a fresh start open the successor's turn, and only that one", () => {
    const chat = `a-${Math.random()}`;
    const heldSends = [
      { message_id: "m-1", text: "Find what's using disk space" },
      { message_id: "m-2", text: "and this" },
    ];
    push([
      chatSnapshotFixture(chat, {
        active_agent: { agent_id: "agent-new" },
        handoff: handoffStateFixture({ phase: "switching", is_fresh_start: true, held_sends: heldSends }),
      }),
    ]);
    push([chatSnapshotFixture(chat, { active_agent: { agent_id: "agent-new" } })]);
    expect(getOutgoingMessages(chat).map((o) => [o.content, o.opensTurn])).toEqual([
      ["Find what's using disk space", true],
      ["and this", false],
    ]);
  });

  it("opens no turn with the held sends back from a handoff whose prompt opens the successor's turn", () => {
    const chat = `a-${Math.random()}`;
    push([
      chatSnapshotFixture(chat, {
        active_agent: { agent_id: "agent-new" },
        handoff: handoffStateFixture({ phase: "switching", held_sends: [{ message_id: "m-2", text: "and this" }] }),
      }),
    ]);
    push([chatSnapshotFixture(chat, { active_agent: { agent_id: "agent-new" } })]);
    expect(getOutgoingMessages(chat).map((o) => [o.content, o.opensTurn])).toEqual([["and this", false]]);
  });

  it("does nothing for a chat that ends a push without ever having switched", () => {
    const chat = `a-${Math.random()}`;
    push([chatSnapshotFixture(chat)]);
    expect(getOutgoingMessages(chat)).toEqual([]);
  });
});
