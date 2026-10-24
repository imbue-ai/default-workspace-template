/**
 * The reply an agent is writing that its transcript does not hold yet.
 *
 * Claude records a reply only once it has finished writing it, so the chat app reads the
 * reply off the agent's terminal while it streams and sends it on the chat's stream as
 * `assistant_draft` frames (the backend's `reply_drafts.py`). A frame names the transcript
 * event the draft follows: the page shows the draft at the end of its transcript only while
 * that event is still the last one it holds, so the reply landing (or anything else landing)
 * retires the draft at once, without waiting for the frame that clears it.
 */

import m from "mithril";
import type { AssistantMessageEvent } from "./Response";

export const REPLY_DRAFT_FRAME_TYPE = "assistant_draft";

/** One `assistant_draft` frame; `text` is empty when the agent has no draft any more. */
export interface ReplyDraftFrame {
  type: typeof REPLY_DRAFT_FRAME_TYPE;
  agent_id: string;
  after_event_id: string;
  text: string;
}

interface HeldReplyDraft {
  afterEventId: string;
  event: AssistantMessageEvent;
}

const draftByChat = new Map<string, HeldReplyDraft>();

export function isReplyDraftFrame(raw: { type?: string }): raw is ReplyDraftFrame {
  return raw.type === REPLY_DRAFT_FRAME_TYPE;
}

/** Hold the frame's draft for its chat (or drop the chat's draft, for an empty one) and redraw. */
export function applyReplyDraftFrame(chatId: string, frame: ReplyDraftFrame): void {
  if (frame.text === "") {
    if (!draftByChat.delete(chatId)) {
      return;
    }
  } else {
    draftByChat.set(chatId, { afterEventId: frame.after_event_id, event: draftEvent(chatId, frame) });
  }
  m.redraw();
}

/** Forget the chat's draft (its stream closed). */
export function clearReplyDraft(chatId: string): void {
  draftByChat.delete(chatId);
}

/** The draft to show after the transcript, when the event it follows is the last one held. */
export function shownReplyDraft(chatId: string, lastEventId: string | null): AssistantMessageEvent | null {
  const held = draftByChat.get(chatId);
  if (held === undefined || held.afterEventId !== (lastEventId ?? "")) {
    return null;
  }
  return held.event;
}

/** The draft as a reply the transcript views render like any other: prose, no tool calls, not yet stopped. */
function draftEvent(chatId: string, frame: ReplyDraftFrame): AssistantMessageEvent {
  return {
    type: "assistant_message",
    event_id: `reply-draft-${chatId}`,
    timestamp: new Date().toISOString(),
    source: REPLY_DRAFT_FRAME_TYPE,
    agent_id: frame.agent_id,
    model: "",
    text: frame.text,
    tool_calls: [],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
}
