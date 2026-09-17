/**
 * The prompt a handoff's successor is started with, as the wire carries it: a chip under the
 * backend's ``HANDOFF_PROMPT_LABEL``. The transcript walk folds it into the handoff node, and the
 * optimistic-send layer must not take it for a sent message's arrival.
 */

import type { UserMessageEvent } from "./Response";

const HANDOFF_PROMPT_LABEL = "Handoff prompt";

export function isHandoffPromptChip(event: Pick<UserMessageEvent, "display" | "display_label">): boolean {
  return event.display === "chip" && event.display_label === HANDOFF_PROMPT_LABEL;
}
