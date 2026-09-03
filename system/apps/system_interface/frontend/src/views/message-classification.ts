/**
 * Classification of transcript `user_message` content and tool calls, shared by
 * the turn-grouping layer (placement) and the rendering layer (appearance).
 *
 * The DECISIONS are made backend-side now: every harness's parser runs the shared
 * detector table (`harnesses/message_display.py`) and ships the result on the wire
 * as the event's `display` / `display_label` / `display_body` / `resolution`
 * fields (and a tool call's `display`). This module only MAPS those fields onto
 * the frontend's `UserMessageKind` catalogue (see message-kinds.ts for how each
 * kind renders) -- it never sniffs message text or tool input, and adding a new
 * specially-rendered message is a backend rule, not a frontend regex.
 */

import type { ToolCall, UserMessageEvent } from "../models/Response";
import { KIND_SPEC, Rail, UserMessageKind } from "./message-kinds";

/** The slice of a user_message the classification reads. */
export type ClassifiableUserMessage = Pick<UserMessageEvent, "content" | "display" | "display_label" | "display_body">;

/** The result of classifying a user_message. */
export interface UserMessageClass {
  kind: UserMessageKind;
  /** Chip label for `SystemChip`; for `SkillExpansion` the skill name; else null. */
  label: string | null;
  /**
   * The text to DISPLAY. For most kinds this equals the original content; for a
   * wrapped kind (e.g. a browser-fleet nudge) the backend supplies the inner text
   * without the sentinel via `display_body`.
   */
  body: string;
}

/**
 * Map a user_message's backend render decision onto a `UserMessageKind`.
 * Grouping and rendering both call this and act on the kind alone.
 *
 * A `permission_resolution` deliberately maps to `UserPrompt` here: the verdict
 * only suppresses the bubble when the timeline walk correlates it to an earlier,
 * still-pending permission card (see `resolutionOf` + turn-grouping); an
 * uncorrelated one renders as an ordinary message.
 */
export function classifyUserMessage(event: ClassifiableUserMessage): UserMessageClass {
  const content = event.content || "";
  switch (event.display) {
    case "hidden":
      return { kind: UserMessageKind.Hidden, label: null, body: content };
    case "chip":
      return {
        kind: UserMessageKind.SystemChip,
        label: event.display_label ?? null,
        body: event.display_body ?? content,
      };
    case "skill_expansion":
      return { kind: UserMessageKind.SkillExpansion, label: event.display_label ?? null, body: content };
    case "status":
      return {
        kind: UserMessageKind.StatusMessage,
        label: event.display_label ?? (event.display_body !== undefined ? content : null),
        body: event.display_body ?? content,
      };
    default:
      return { kind: UserMessageKind.UserPrompt, label: null, body: content };
  }
}

// --- Thin semantic helpers over classifyUserMessage -------------------------
// Kept as named predicates because callers ask a specific structural question;
// all derive from the single classification above.

/**
 * True for a user_message that is NOT a genuine human turn and so must not be
 * treated as a turn boundary -- folding one of these into the running turn keeps
 * a single logical turn from being split into several visible ones.
 */
export function isNonBoundaryUserMessage(event: ClassifiableUserMessage): boolean {
  // Derived from the KIND_SPEC registry (its `boundary` column) so the boundary
  // rule lives in exactly one place -- the spec.
  return !KIND_SPEC[classifyUserMessage(event).kind].boundary;
}

/** True when the message folds into the current turn as a collapsed chip (rather
 *  than being dropped): the SystemChip kinds. */
export function isSystemChipUserMessage(event: ClassifiableUserMessage): boolean {
  return classifyUserMessage(event).kind === UserMessageKind.SystemChip;
}

/** True when the message is a subtle inline status message. */
export function isStatusUserMessage(event: ClassifiableUserMessage): boolean {
  return classifyUserMessage(event).kind === UserMessageKind.StatusMessage;
}

/** True when the content is a skill expansion (its body is folded into the
 *  preceding Skill tool-call block; see buildToolResultsWithSkillExpansions). */
export function isSkillExpansionUserMessage(event: ClassifiableUserMessage): boolean {
  return classifyUserMessage(event).kind === UserMessageKind.SkillExpansion;
}

/** True when the message produces NO row on the user rail -- either fully hidden
 *  (`/welcome`, an is_meta injection) or relocated into an assistant-side block
 *  (skill expansion). The rendering/rows layers use this to skip emitting a row. */
export function isHiddenUserMessage(event: ClassifiableUserMessage): boolean {
  return KIND_SPEC[classifyUserMessage(event).kind].rail !== Rail.User;
}

// --- Permission REQUEST (a tool call) ---------------------------------------

/** True when a tool call is an agent permission request (a POST to the reserved
 *  latchkey host). The backend recognises it from the UNTRUNCATED input the
 *  moment it is issued -- even while it is still pending with no result yet,
 *  which is exactly when the user most needs to see and act on it. */
export function isPermissionRequestCall(tc: ToolCall): boolean {
  return tc.display === "permission_request";
}

// --- Permission RESOLUTION (a user_message verdict) -------------------------

/** The outcome of a permission request, once it has been resolved:
 *   - "granted"/"denied": the user made a decision.
 *   - "error": the request could not be completed (e.g. the user's sign-in flow
 *     did not finish) -- not a decision, so it reads distinctly on the card. */
export type PermissionResolution = "granted" | "denied" | "error";

/** The verdict a resolution message carries, or null when the message is not one.
 *  The backend matches the latchkey handlers' injected phrasing (authored in the
 *  mngr repo) and stamps `display: "permission_resolution"` + `resolution`; the
 *  walk consults this only while a permission request is actually awaiting a
 *  decision, and writes the verdict onto the earlier card. */
export function resolutionOf(event: Pick<UserMessageEvent, "display" | "resolution">): PermissionResolution | null {
  if (event.display !== "permission_resolution") {
    return null;
  }
  const verdict = event.resolution;
  return verdict === "granted" || verdict === "denied" || verdict === "error" ? verdict : null;
}

/** The id of the request a resolution message resolves, or null when the message
 *  is not a resolution or carries no id (a notice recorded before request-id
 *  embedding shipped, which the walk instead correlates by arrival order). */
export function resolutionRequestIdOf(event: Pick<UserMessageEvent, "display" | "request_id">): string | null {
  if (event.display !== "permission_resolution") {
    return null;
  }
  return event.request_id ?? null;
}
