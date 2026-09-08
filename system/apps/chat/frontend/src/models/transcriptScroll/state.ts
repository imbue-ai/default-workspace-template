/**
 * The two transcript-scroll state machines as pure, exhaustive reducers.
 *
 * Scroll position:
 *   FOLLOW          + USER_SCROLLED(atTail=false) -> USER_CONTROLLED   [any intent to scroll up]
 *   FOLLOW          + EVENTS_APPENDED             -> FOLLOW            [new messages]
 *   USER_CONTROLLED + USER_SCROLLED(atTail=true)  -> FOLLOW            [scrolled all the way down]
 *   USER_CONTROLLED + USER_SCROLLED(atTail=false) -> USER_CONTROLLED   [re-anchor]
 *   USER_CONTROLLED + MESSAGE_SENT                -> FOLLOW            [send snaps to tail]
 *   *               + JUMPED_TO_INDEX             -> USER_CONTROLLED   [scrollbar jump landed]
 *
 * Scrollbar interaction:
 *   ELSEWHERE + SCROLLBAR_ENGAGED -> SCROLLBAR (mapping frozen at engage)
 *   SCROLLBAR + SCROLLBAR_ENGAGED -> SCROLLBAR (frozen mapping kept: keep scrubbing)
 *   *         + OTHER_INTERACTION -> ELSEWHERE
 *
 * Both reducers return the same state reference when nothing changed, so callers
 * can detect transitions with identity comparison.
 */

import type {
  ScrollPositionEvent,
  ScrollPositionState,
  ScrollbarInteractionEvent,
  ScrollbarInteractionState,
} from "./types";

export const FOLLOW_STATE: ScrollPositionState = { kind: "FOLLOW" };
export const ELSEWHERE_STATE: ScrollbarInteractionState = { kind: "ELSEWHERE" };

function assertNever(value: never): never {
  throw new Error(`Unhandled case: ${JSON.stringify(value)}`);
}

export function reduceScrollPosition(state: ScrollPositionState, event: ScrollPositionEvent): ScrollPositionState {
  switch (event.kind) {
    case "USER_SCROLLED":
      if (event.atTail) {
        return state.kind === "FOLLOW" ? state : FOLLOW_STATE;
      }
      return { kind: "USER_CONTROLLED", anchor: event.anchor };
    case "EVENTS_APPENDED":
      return state;
    case "MESSAGE_SENT":
      return state.kind === "FOLLOW" ? state : FOLLOW_STATE;
    case "JUMPED_TO_INDEX":
      return { kind: "USER_CONTROLLED", anchor: event.anchor };
    default:
      return assertNever(event);
  }
}

export function reduceScrollbarInteraction(
  state: ScrollbarInteractionState,
  event: ScrollbarInteractionEvent,
): ScrollbarInteractionState {
  switch (event.kind) {
    case "SCROLLBAR_ENGAGED":
      return state.kind === "SCROLLBAR" ? state : { kind: "SCROLLBAR", frozen: event.mappingAtEngage };
    case "OTHER_INTERACTION":
      return state.kind === "ELSEWHERE" ? state : ELSEWHERE_STATE;
    default:
      return assertNever(event);
  }
}
