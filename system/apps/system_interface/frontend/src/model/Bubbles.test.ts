import { describe, expect, it } from "vitest";
import {
  BUBBLE_LIFETIME_MS,
  WORKSPACE_AGENT_KEY,
  foldedLabel,
  nextExpiryOf,
  withBubble,
  withoutChat,
  withoutExpired,
  withoutSpeaker,
} from "./Bubbles";
import type { BubbleWire } from "./records";

function wire(speaker: string, text: string, overrides: Partial<BubbleWire> = {}): BubbleWire {
  return {
    speaker,
    chat_id: "chat-1",
    chat_title: "Lounge",
    text,
    message_id: `m-${text}`,
    mentions: [],
    ...overrides,
  };
}

const VIEWER = "user:user-josh-0001";

describe("withBubble (S11)", () => {
  it("replaces a person's bubble and restarts its timer, and appends to the agent's within its lifetime", () => {
    let stack = withBubble([], wire("user:bob", "one"), VIEWER, 1000);
    stack = withBubble(stack, wire("user:bob", "two"), VIEWER, 2000);
    expect(stack.map((bubble) => [bubble.speaker, bubble.lines, bubble.changedAt])).toEqual([
      ["user:bob", ["two"], 2000],
    ]);

    stack = withBubble(stack, wire(WORKSPACE_AGENT_KEY, "a"), VIEWER, 2500);
    stack = withBubble(stack, wire(WORKSPACE_AGENT_KEY, "b"), VIEWER, 3000);
    expect(stack.map((bubble) => bubble.speaker)).toEqual(["user:bob", WORKSPACE_AGENT_KEY]);
    expect(stack[1].lines).toEqual(["a", "b"]);
    expect(stack[1].changedAt).toBe(3000);
  });

  it("folds the agent's lines past three into a count, and stacks different speakers in arrival order", () => {
    let stack: ReturnType<typeof withBubble> = [];
    for (const text of ["a", "b", "c", "d", "e"])
      stack = withBubble(stack, wire(WORKSPACE_AGENT_KEY, text), VIEWER, 1);
    expect(stack[0].lines).toEqual(["c", "d", "e"]);
    expect(stack[0].foldedCount).toBe(2);
    expect(foldedLabel(2)).toBe("and 2 more");
    stack = withBubble(stack, wire("user:carol", "hi"), VIEWER, 2);
    expect(stack.map((bubble) => bubble.speaker)).toEqual([WORKSPACE_AGENT_KEY, "user:carol"]);
  });

  it("marks a mention of the viewer, which never times out, until its chat is opened or it is dismissed", () => {
    const stack = withBubble([], wire("user:bob", "@Josh look", { mentions: [VIEWER] }), VIEWER, 1000);
    expect(stack[0].isMention).toBe(true);
    expect(withoutExpired(stack, 1000 + BUBBLE_LIFETIME_MS * 10)).toEqual(stack);
    expect(nextExpiryOf(stack)).toBeNull();
    expect(withoutChat(stack, "chat-1")).toEqual([]);
    expect(withoutSpeaker(stack, "user:bob")).toEqual([]);
    // Somebody else's mention is not the viewer's.
    expect(withBubble([], wire("user:bob", "@Carol", { mentions: ["user:carol"] }), VIEWER, 1)[0].isMention).toBe(
      false,
    );
  });

  it("times a bubble out five seconds after its last change", () => {
    let stack = withBubble([], wire("user:bob", "one"), VIEWER, 1000);
    stack = withBubble(stack, wire("user:carol", "two"), VIEWER, 3000);
    expect(nextExpiryOf(stack)).toBe(1000 + BUBBLE_LIFETIME_MS);
    expect(withoutExpired(stack, 1000 + BUBBLE_LIFETIME_MS).map((bubble) => bubble.speaker)).toEqual(["user:carol"]);
    expect(withoutExpired(stack, 3000 + BUBBLE_LIFETIME_MS)).toEqual([]);
  });
});
