/**
 * The coach mark at the chat list's New chat button, which a chat page asks the root for (its "Work in parallel by
 * starting a new chat" suggestion): the button is the root's, so only the root can point at it. The button pulses
 * while the mark is up. In the phone layout the list is in a drawer, which opens first.
 */

import { showCoachMark } from "@imbue/workspace-ui/src/components/CoachMark";

export const NEW_CHAT_COACH_TEXT =
  "Start a new chat here any time. Each chat has its own agent, so several can work for you at once.";
/** What the button wears while the mark points at it (``style.css``). */
export const NEW_CHAT_PULSE_CLASS = "coach-pulse";
// The New chat button (``ChatRail.ts``): the rail's, or the drawer header's.
const NEW_CHAT_BUTTON_SELECTOR = ".chat-rail-new";

/** Whether the animation ends by itself. One that repeats forever, as the button's pulse does while a mark is up,
 *  never finishes: waiting for it would hold a second mark back until the first is taken down. */
function endsByItself(animation: Animation): boolean {
  return animation.effect?.getComputedTiming().endTime !== Infinity;
}

/** Once nothing that moves ``element`` is still animating: its own animations and those of what contains it. */
async function whenAtRest(element: HTMLElement): Promise<void> {
  const running: Promise<unknown>[] = [];
  for (let node: HTMLElement | null = element; node !== null; node = node.parentElement) {
    if (typeof node.getAnimations !== "function") continue;
    for (const animation of node.getAnimations()) {
      if (endsByItself(animation)) running.push(animation.finished);
    }
  }
  // An animation that is cancelled rejects, and leaves the element at rest all the same.
  await Promise.allSettled(running);
}

/**
 * Point the coach mark at the New chat button. ``revealList`` puts the chat list on screen where it is not (the
 * phone layout's drawer), drawn by the time it returns; the mark waits for the drawer's slide to end, so it is
 * placed at where the button comes to rest.
 */
export async function showNewChatCoachMark(revealList: () => void, root: ParentNode = document): Promise<void> {
  revealList();
  const button = root.querySelector<HTMLElement>(NEW_CHAT_BUTTON_SELECTOR);
  if (button === null) return;
  await whenAtRest(button);
  if (!button.isConnected) return;
  showCoachMark({ anchor: button, text: NEW_CHAT_COACH_TEXT, pulseClass: NEW_CHAT_PULSE_CLASS });
}
