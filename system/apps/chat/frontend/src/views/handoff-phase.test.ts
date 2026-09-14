import { describe, expect, it, vi } from "vitest";

vi.mock("../models/Chats", () => ({ getChatById: (id: string) => chats.get(id) }));

import type { ChatSnapshot } from "../models/Chats";
import { chatSnapshotFixture, handoffStateFixture } from "../models/chatSnapshotFixture";
import { handoffComposerPlaceholder, handoffPhaseText, isHandoffCancellable } from "./handoff-phase";
import { heldSendMessageIds, renderHeldSends } from "./HeldSendView";

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

describe("the held-send bubbles", () => {
  it("renders every held message from the snapshot, captioned with the phase, and names their ids", () => {
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
    expect(text).toContain("Claude is writing a summary…");
    expect(text).not.toContain("Sending…");
    expect([...heldSendMessageIds("agent-1")]).toEqual(["m-1", "m-2"]);
  });

  it("renders nothing for a chat that is not switching", () => {
    chats.set("agent-2", chatSnapshotFixture("agent-2"));
    expect(renderHeldSends("agent-2")).toEqual([]);
    expect(renderHeldSends("agent-unknown")).toEqual([]);
    expect(heldSendMessageIds("agent-2").size).toBe(0);
  });
});
