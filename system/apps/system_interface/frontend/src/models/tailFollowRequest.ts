/**
 * A tiny cross-component signal: "snap this agent's transcript to the bottom and
 * follow the tail again."
 *
 * The Shoulder-tap button (QueuedMessageView) and the transcript scroll controller
 * (ChatPanel) live in different components. When the user taps, they want to watch
 * the interrupted / merged turn land, so we ask the transcript to re-engage tail
 * following even if they had scrolled up. QueuedMessageView `request`s it; ChatPanel
 * `consume`s it on its next redraw and calls `scroll.followTail`.
 */

const pendingByAgent = new Set<string>();

/** Ask the given agent's transcript to snap to the bottom on its next redraw. */
export function requestTailFollow(agentId: string): void {
  pendingByAgent.add(agentId);
}

/** Consume a pending request for this agent (true iff one was set). One-shot. */
export function consumeTailFollow(agentId: string): boolean {
  return pendingByAgent.delete(agentId);
}
