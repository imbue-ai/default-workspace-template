/** Transcript events for tests, as the chat app streams them: an agent's finished reply, and a user message. */

import type { TranscriptEvent } from "./Response";

/** An agent's reply saying ``text``, its turn ended. */
export function assistantMessageFixture(eventId: string, text: string): TranscriptEvent {
  return {
    type: "assistant_message",
    event_id: eventId,
    timestamp: "2026-11-21T10:00:00Z",
    source: "test",
    model: "test-model",
    text,
    tool_calls: [],
    stop_reason: "end_turn",
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
    api_error_message: null,
    is_error_text_raw: false,
  };
}

/** A user message saying ``content``: a person's own, or with ``display`` one the harness wrote in their name. */
export function userMessageFixture(eventId: string, content: string, display?: "skill_expansion"): TranscriptEvent {
  return {
    type: "user_message",
    event_id: eventId,
    timestamp: "2026-11-21T10:01:00Z",
    source: "test",
    role: "user",
    content,
    ...(display === undefined ? {} : { display }),
  };
}
