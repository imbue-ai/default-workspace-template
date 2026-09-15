// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: (id: string) => chats.get(id),
}));

import type { ChatSnapshot } from "../models/Chats";
import { isHandoffCancellable } from "../models/Chats";
import { chatSnapshotFixture, handoffStateFixture, rebindStateFixture } from "../models/chatSnapshotFixture";
import { appendEvents } from "../models/Response";
import { handoffComposerPlaceholder, handoffPhaseText } from "./handoff-phase";
import { renderHeldSends } from "./HeldSendView";

const chats = new Map<string, ChatSnapshot>();

describe("the words for a chat switching harness", () => {
  it("follows the phase, naming the harness each phase is about", () => {
    expect(handoffPhaseText(handoffStateFixture({ phase: "draining" }), "claude")).toBe("Wrapping up with Claude…");
    expect(handoffPhaseText(handoffStateFixture({ phase: "summarizing" }), "claude")).toBe(
      "Claude is writing a summary…",
    );
    expect(handoffPhaseText(handoffStateFixture({ phase: "switching" }), "claude")).toBe("Starting Codex…");
    expect(handoffPhaseText(handoffStateFixture({ phase: "failed" }), "claude")).toBe("Could not start Codex");
    expect(handoffPhaseText(handoffStateFixture({ target_harness: "pi-coding", phase: "switching" }), "codex")).toBe(
      "Starting Pi…",
    );
  });

  it("allows a cancel only until the old agent is stopped", () => {
    expect(isHandoffCancellable(handoffStateFixture({ phase: "draining" }))).toBe(true);
    expect(isHandoffCancellable(handoffStateFixture({ phase: "summarizing" }))).toBe(true);
    expect(isHandoffCancellable(handoffStateFixture({ phase: "switching" }))).toBe(false);
    expect(isHandoffCancellable(handoffStateFixture({ phase: "failed" }))).toBe(false);
  });

  it("tells the composer where a message typed now goes", () => {
    expect(handoffComposerPlaceholder(handoffStateFixture())).toBe(
      "Type a message; it is delivered once Codex is ready…",
    );
  });
});

describe("the words for a chat changing account in place", () => {
  it("keeps the harness and names the account the agent restarts on", () => {
    expect(handoffPhaseText(rebindStateFixture({ phase: "draining" }), "claude")).toBe("Wrapping up with Claude…");
    expect(handoffPhaseText(rebindStateFixture({ phase: "restarting" }), "claude")).toBe(
      "Restarting Claude on Anthropic 2 (Claude Code)…",
    );
    expect(handoffPhaseText(rebindStateFixture({ phase: "failed" }), "claude")).toBe(
      "Could not restart Claude on Anthropic 2 (Claude Code)",
    );
    expect(handoffComposerPlaceholder(rebindStateFixture())).toBe(
      "Type a message; it is delivered once Anthropic 2 (Claude Code) is ready…",
    );
  });

  it("can never be called off: the agent restarts as soon as the switch is confirmed", () => {
    expect(isHandoffCancellable(rebindStateFixture({ phase: "draining" }))).toBe(false);
    expect(isHandoffCancellable(rebindStateFixture({ phase: "restarting" }))).toBe(false);
    expect(isHandoffCancellable(rebindStateFixture({ phase: "failed" }))).toBe(false);
  });
});

describe("the held-send bubbles", () => {
  it("renders every held message from the snapshot, captioned like any send", () => {
    chats.set(
      "agent-1",
      chatSnapshotFixture("agent-1", {
        handoff: handoffStateFixture({
          held_sends: [
            { message_id: "m-1", text: "Carry on in Codex" },
            { message_id: "m-2", text: "and this" },
          ],
        }),
      }),
    );
    const bubbles = renderHeldSends("agent-1");
    expect(bubbles).toHaveLength(2);
    expect(bubbles.map((bubble) => bubble.key)).toEqual(["held-m-1", "held-m-2"]);
    const text = JSON.stringify(bubbles);
    expect(text).toContain("Carry on in Codex");
    expect(text).toContain("and this");
    // The switch's progress is the handoff node's to tell, not the bubbles'.
    expect(text).toContain("Sending…");
    expect(text).not.toContain("Claude is writing a summary…");
  });

  it("stands the confirming message down once the switch marker carrying it is on the transcript", () => {
    // The snapshot keeps re-listing the confirming message for as long as the switch lasts, but
    // the marker lands while it is still converging and is the message's real form: without the
    // stand-down the page would paint the same text as a bubble and as the opening turn at once.
    const chat = `agent-carried-${Math.random()}`;
    chats.set(
      chat,
      chatSnapshotFixture(chat, {
        handoff: handoffStateFixture({
          held_sends: [
            { message_id: "trigger-1", text: "Carry on in Codex" },
            { message_id: "m-2", text: "and this" },
          ],
        }),
      }),
    );
    expect(renderHeldSends(chat).map((bubble) => bubble.key)).toEqual(["held-trigger-1", "held-m-2"]);

    appendEvents(chat, [
      {
        timestamp: "2026-01-01T00:00:00Z",
        type: "agent_switch",
        event_id: "sw1",
        source: "chat",
        from_agent_id: "agent-old",
        to_agent_id: "agent-new",
        from_harness: "claude",
        to_harness: "codex",
        seq: 1,
        message_id: "trigger-1",
        message: "Carry on in Codex",
      },
    ]);
    expect(renderHeldSends(chat).map((bubble) => bubble.key)).toEqual(["held-m-2"]);
  });

  it("captions a rebind's held messages the same way", () => {
    chats.set("agent-3", chatSnapshotFixture("agent-3", { handoff: rebindStateFixture() }));
    const text = JSON.stringify(renderHeldSends("agent-3"));
    expect(text).toContain("Carry on on the other account");
    expect(text).toContain("Sending…");
  });

  it("renders nothing for a chat that is not switching", () => {
    chats.set("agent-2", chatSnapshotFixture("agent-2"));
    expect(renderHeldSends("agent-2")).toEqual([]);
    expect(renderHeldSends("agent-unknown")).toEqual([]);
  });
});
