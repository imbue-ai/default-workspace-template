/**
 * Speech bubbles (the multiplayer spec, section 6.5, S11): what somebody just said, hanging over their tray entry.
 *
 * One bubble per speaker, debounced: a person's new message replaces their bubble and restarts its timer; the
 * agent's new message within its bubble's lifetime is appended to it ("and 2 more" past three lines) and restarts
 * the timer; bubbles of different speakers stack upward in arrival order. A bubble lives `BUBBLE_LIFETIME_MS` after
 * its last change, unless it is a mention of the viewer, which stays until dismissed or its chat is opened. Every
 * message raises one on every client but the speaker's own (the shell leaves those out) and the clients looking at
 * the chat it was said in (the store leaves those out on receipt -- a mention of the viewer still rises there).
 * The reducer is pure; the timers are the model's.
 */

import type { BubbleWire } from "./records";

export const BUBBLE_LIFETIME_MS = 5_000;
/** How many lines the agent's bubble shows before the rest fold into "and N more". */
export const BUBBLE_LINE_LIMIT = 3;
/** The workspace agent's participant key (the multiplayer spec, section 4.1). */
export const WORKSPACE_AGENT_KEY = "workspace-agent";

export interface Bubble {
  readonly speaker: string;
  readonly chatId: string;
  readonly chatTitle: string;
  /** The lines shown, newest last; a person's bubble holds one. */
  readonly lines: readonly string[];
  /** How many lines past `BUBBLE_LINE_LIMIT` the agent's bubble folded. */
  readonly foldedCount: number;
  readonly messageId: string;
  /** Whether the message mentions the viewer: the bubble is marked and never times out. */
  readonly isMention: boolean;
  /** When the bubble last changed, in ms since the epoch: what the lifetime counts from. */
  readonly changedAt: number;
}

/** The bubbles in arrival order (the first arrived first; the view stacks them upward). */
export type BubbleStack = readonly Bubble[];

/** Add a wire bubble to the stack (S11): the speaker's bubble replaced (a person) or appended to (the agent) when
 *  one is up, else a new one at the end. */
export function withBubble(stack: BubbleStack, wire: BubbleWire, viewer: string | null, now: number): BubbleStack {
  const isMention = viewer !== null && wire.mentions.includes(viewer);
  const index = stack.findIndex((bubble) => bubble.speaker === wire.speaker);
  const previous = index === -1 ? null : stack[index];
  const isAppended = previous !== null && wire.speaker === WORKSPACE_AGENT_KEY;
  const lines = isAppended ? [...previous.lines, wire.text] : [wire.text];
  const shown = lines.slice(-BUBBLE_LINE_LIMIT);
  const next: Bubble = {
    speaker: wire.speaker,
    chatId: wire.chat_id,
    chatTitle: wire.chat_title,
    lines: shown,
    foldedCount: (isAppended ? previous.foldedCount : 0) + lines.length - shown.length,
    messageId: wire.message_id,
    isMention: isMention || (previous !== null && previous.isMention && previous.chatId === wire.chat_id),
    changedAt: now,
  };
  if (previous === null) return [...stack, next];
  return stack.map((bubble, at) => (at === index ? next : bubble));
}

/** The stack without the bubbles whose lifetime has run out; a mention of the viewer never does. */
export function withoutExpired(stack: BubbleStack, now: number): BubbleStack {
  return stack.filter((bubble) => bubble.isMention || now - bubble.changedAt < BUBBLE_LIFETIME_MS);
}

/** The stack without one speaker's bubble (a dismissal, a click). */
export function withoutSpeaker(stack: BubbleStack, speaker: string): BubbleStack {
  return stack.filter((bubble) => bubble.speaker !== speaker);
}

/** The stack without the bubbles pointing at `chatId`: the chat was opened, so its mentions are seen. */
export function withoutChat(stack: BubbleStack, chatId: string): BubbleStack {
  return stack.filter((bubble) => bubble.chatId !== chatId);
}

/** When the next bubble times out, in ms since the epoch, or null while none will. */
export function nextExpiryOf(stack: BubbleStack): number | null {
  const expiries = stack.filter((bubble) => !bubble.isMention).map((bubble) => bubble.changedAt + BUBBLE_LIFETIME_MS);
  return expiries.length === 0 ? null : Math.min(...expiries);
}

/** The text a bubble folds past its line limit into: "and 2 more". */
export function foldedLabel(count: number): string {
  return `and ${count} more`;
}
