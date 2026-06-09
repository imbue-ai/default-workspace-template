/**
 * Event store for common transcript events.
 * Replaces the LLM response model with events fetched from session files.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export interface SubagentMetadata {
  agent_type: string;
  description: string;
  session_id: string;
}

export interface ToolCall {
  tool_call_id: string;
  tool_name: string;
  input_preview: string;
  // For Agent tool calls: the description and subagent_type from the tool input, present
  // as soon as the call appears so the rich card can render before the subagent session is
  // linked. subagent_metadata (with the session_id for the click-through) is filled in once
  // the linkage is resolved.
  description?: string;
  subagent_type?: string;
  subagent_metadata?: SubagentMetadata;
}

/**
 * Status vocabulary mirrored from the tk ticket tracker:
 *   - "open"        -> rendered as "pending" in the chat progress UI
 *   - "in_progress" -> rendered as "active"
 *   - "closed"      -> rendered as "done"
 * There is no failed state by design (every ticket terminates as closed
 * with a summary; see CLAUDE.md "Task management" in the FCT side).
 */
export type TaskEventStatus = "open" | "in_progress" | "closed";

/**
 * Fields shared by every event, regardless of `type`. The `/events` stream is
 * the session transcript (user/assistant/tool_result); these are the only
 * transport-level fields guaranteed on all variants. (tk step state is not in
 * this stream -- it ships as a separate enrichment snapshot, see
 * StepEnrichment.)
 */
export interface BaseTranscriptEvent {
  timestamp: string;
  event_id: string;
  source: string;
  // message_uuid is always set for transcript events; session_id is set only
  // when the backend knows which session file an event came from, so it is
  // conditional on every variant.
  message_uuid?: string;
  session_id?: string;
}

/**
 * A message from the user (or a hook/system message rendered as one).
 * session_parser only emits this event when there is real user text, so
 * `content` is always present and non-empty.
 */
export interface UserMessageEvent extends BaseTranscriptEvent {
  type: "user_message";
  role: string;
  content: string;
}

/**
 * A model turn: prose text and/or tool calls. Every field below is always
 * present in the backend's emit (`session_parser._parse_assistant_message`);
 * `text` may be empty and `tool_calls` may be empty, but the keys are always
 * there, and `stop_reason` / `usage` are present-but-nullable.
 */
export interface AssistantMessageEvent extends BaseTranscriptEvent {
  type: "assistant_message";
  model: string;
  text: string;
  tool_calls: ToolCall[];
  stop_reason: string | null;
  usage: {
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number | null;
    cache_write_tokens: number | null;
  } | null;
  // True when the text matches a known Claude auth-error pattern.
  is_auth_error: boolean;
}

/**
 * The result of a single tool call, keyed back by `tool_call_id`.
 * session_parser skips emitting a tool_result with no tool_use_id, so when
 * one exists `tool_call_id` is always a non-empty string.
 */
export interface ToolResultEvent extends BaseTranscriptEvent {
  type: "tool_result";
  tool_call_id: string;
  tool_name: string;
  output: string;
  is_error: boolean;
}

/**
 * Per-step enrichment, keyed by ticket id, delivered as a snapshot alongside
 * the transcript (the `step_enrichment` field on the events response and the
 * `step_enrichment` SSE message). tk owns this side-table: canonical title,
 * close summary, current status, and the creation timestamp (used only to
 * order not-yet-started steps). The progress view derives all structure from
 * the transcript and joins this in by id; it never determines order or
 * grouping.
 */
export interface StepEnrichment {
  title: string;
  summary: string | null;
  status: TaskEventStatus;
  created_at: string;
}

/**
 * A single entry in the transcript event stream, discriminated by `type`.
 * Narrow on `event.type` before touching variant-specific fields.
 */
export type TranscriptEvent = UserMessageEvent | AssistantMessageEvent | ToolResultEvent;

// For hook compatibility
export interface ResponseItem {
  id: string;
  model: string;
  prompt: string | null;
  system: string | null;
  response: string;
  conversation_id: string;
  datetime_utc: string;
  duration_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
}

interface EventsResponse {
  events: TranscriptEvent[];
  // Full, unpaginated snapshot of the agent's step enrichment keyed by ticket
  // id. Always complete regardless of where the transcript window is, so a
  // freshly-loaded tail still has titles/summaries for every visible step.
  step_enrichment?: Record<string, StepEnrichment>;
  // Global index of the first returned event within the full transcript, and the
  // transcript's total length. Together they place the loaded window in the whole
  // conversation: the client sizes the scrollbar for `total` and derives whether
  // more history exists above (offset > 0) and below (offset + events < total).
  offset?: number;
  total?: number;
}

const BACKFILL_PAGE_SIZE = 50;

// Upper bound on events held client-side per agent. Far above any viewport
// window; bounds JS memory for an arbitrarily long conversation while leaving
// generous scrollback resident. Eviction (see evictOldEvents) only trims the
// oldest events and only when the caller is following the live tail.
export const MAX_HELD_EVENTS = 1500;
// Target size to trim down to when evicting, so eviction runs in batches rather
// than on every appended event once at the cap.
export const EVICT_TARGET_EVENTS = 1000;

// The held events are a single contiguous window of the full transcript. Its
// position is tracked by `firstOffsetByAgent` (the global index of events[0]) and
// the full length by `totalEventCountByAgent`; everything else -- whether more
// history exists above/below, the scrollbar size -- is derived from those two.
// The window can sit anywhere (the live tail is just the case where it ends at
// `total`), so it pages in both directions and can be replaced wholesale by a
// jump to an arbitrary offset.
const eventsByAgent: Record<string, TranscriptEvent[]> = {};
// Persistent per-agent index from event_id to the stored event object,
// mirroring eventsByAgent. Gives O(1) dedup on append/prepend (instead of
// rebuilding a Set on every SSE delivery) and O(1) lookup of an already-stored
// event so a re-broadcast can upgrade it in place (see appendEvents).
const eventByIdByAgent: Record<string, Map<string, TranscriptEvent>> = {};
const notFoundAgentIds = new Set<string>();
// Global index of the first held event within the full transcript (0 when the
// window starts at the beginning). beforeCount == firstOffset; afterCount ==
// total - (firstOffset + held).
const firstOffsetByAgent: Record<string, number> = {};
// Per-agent step enrichment, keyed by ticket id. Replaced wholesale on each
// snapshot (GET /events and the `step_enrichment` SSE message), never merged.
const enrichmentByAgent: Record<string, Map<string, StepEnrichment>> = {};

// Monotonic per-agent counter bumped on every mutation that changes what the
// transcript renders (events added, removed, or upgraded in place; enrichment
// replaced). The chat view memoizes its turn-grouping on this, so a scroll-only
// redraw -- which changes no data -- reuses the cached sections/rows instead of
// re-walking the whole held transcript every frame (the dominant scroll cost on
// a long conversation).
const renderVersionByAgent: Record<string, number> = {};

function bumpRenderVersion(agentId: string): void {
  renderVersionByAgent[agentId] = (renderVersionByAgent[agentId] ?? 0) + 1;
}

export function getRenderVersion(agentId: string): number {
  return renderVersionByAgent[agentId] ?? 0;
}

// Total events in the full server-side transcript (see EventsResponse.total),
// not just the held window. Used to size the scrollbar for the whole
// conversation while only a window is loaded.
const totalEventCountByAgent: Record<string, number> = {};

/** Global index of the first held event within the full transcript. */
export function getFirstOffset(agentId: string): number {
  return firstOffsetByAgent[agentId] ?? 0;
}

/** Total number of events in the full transcript, for scrollbar sizing. Never
 *  less than the loaded window's end, so the window always fits inside it. */
export function getTotalEventCount(agentId: string): number {
  const windowEnd = getFirstOffset(agentId) + (eventsByAgent[agentId]?.length ?? 0);
  return Math.max(totalEventCountByAgent[agentId] ?? 0, windowEnd);
}

/** Older history exists before the loaded window (the window doesn't start at 0). */
export function hasMoreBefore(agentId: string): boolean {
  return getFirstOffset(agentId) > 0;
}

/** Newer history exists after the loaded window (the window doesn't reach the
 *  live tail) -- true only after a jump/scroll moved the window off the end. */
export function hasMoreAfter(agentId: string): boolean {
  const windowEnd = getFirstOffset(agentId) + (eventsByAgent[agentId]?.length ?? 0);
  return windowEnd < getTotalEventCount(agentId);
}

function idMap(agentId: string): Map<string, TranscriptEvent> {
  let map = eventByIdByAgent[agentId];
  if (map === undefined) {
    map = new Map<string, TranscriptEvent>();
    eventByIdByAgent[agentId] = map;
  }
  return map;
}

export function getEnrichmentForAgent(agentId: string): Map<string, StepEnrichment> {
  return enrichmentByAgent[agentId] ?? new Map();
}

/** Replace an agent's enrichment table from a snapshot. Does not redraw --
 *  callers in a fetch/redraw flow already trigger one; the SSE path redraws
 *  explicitly. */
export function applyEnrichmentSnapshot(agentId: string, snapshot: Record<string, StepEnrichment> | undefined): void {
  enrichmentByAgent[agentId] = new Map(Object.entries(snapshot ?? {}));
  bumpRenderVersion(agentId);
}

export function isConversationNotFound(agentId: string): boolean {
  return notFoundAgentIds.has(agentId);
}

export function getEventsForAgent(agentId: string): TranscriptEvent[] {
  return eventsByAgent[agentId] ?? [];
}

export function getEventCount(agentId: string): number {
  return eventsByAgent[agentId]?.length ?? 0;
}

export function getFirstEventId(agentId: string): string | null {
  const events = eventsByAgent[agentId];
  if (!events || events.length === 0) {
    return null;
  }
  return events[0].event_id;
}

export function getLastEventId(agentId: string): string | null {
  const events = eventsByAgent[agentId];
  if (!events || events.length === 0) {
    return null;
  }
  return events[events.length - 1].event_id;
}

/**
 * Merge late-arriving subagent_metadata from a re-broadcast assistant message
 * onto an already-stored one.
 *
 * A running subagent's parent Agent tool_call is streamed before the subagent's
 * session linkage is known, so it first arrives with no subagent_metadata. The
 * backend re-broadcasts the same assistant_message (same event_id) once linkage
 * lands; without this merge appendEvents would discard the re-broadcast as a
 * duplicate and the plain tool-call block would never upgrade to the rich card.
 *
 * Mutates `prior.tool_calls` in place (matched by tool_call_id) and returns
 * whether anything changed.
 */
function mergeLateSubagentMetadata(prior: TranscriptEvent, incoming: TranscriptEvent): boolean {
  if (prior.type !== "assistant_message" || incoming.type !== "assistant_message") {
    return false;
  }
  const incomingByCallId = new Map<string, ToolCall>();
  for (const tc of incoming.tool_calls ?? []) {
    incomingByCallId.set(tc.tool_call_id, tc);
  }
  let changed = false;
  for (const tc of prior.tool_calls ?? []) {
    if (tc.subagent_metadata !== undefined) {
      continue;
    }
    const incomingTc = incomingByCallId.get(tc.tool_call_id);
    if (incomingTc?.subagent_metadata !== undefined) {
      tc.subagent_metadata = incomingTc.subagent_metadata;
      changed = true;
    }
  }
  return changed;
}

export function appendEvents(agentId: string, newEvents: TranscriptEvent[]): void {
  // Live SSE deltas are new tail events. They only belong in the window when it
  // is tail-anchored (reaches the live end). If the user has jumped to an earlier
  // position (window not at the tail), appending them would break contiguity, so
  // we drop them here -- they are re-fetched via forward paging when the user
  // returns to the tail. A late re-broadcast that upgrades an already-held event
  // in place is still applied regardless of where the window sits.
  const byId = idMap(agentId);
  const existing = eventsByAgent[agentId] ?? [];
  const tailAnchored = !hasMoreAfter(agentId);
  const brandNewEvents: TranscriptEvent[] = [];
  let didMerge = false;
  for (const event of newEvents) {
    const prior = byId.get(event.event_id);
    if (prior === undefined) {
      if (tailAnchored) {
        brandNewEvents.push(event);
        byId.set(event.event_id, event);
      }
    } else if (mergeLateSubagentMetadata(prior, event)) {
      didMerge = true;
    }
  }
  if (brandNewEvents.length > 0) {
    eventsByAgent[agentId] = [...existing, ...brandNewEvents];
    // Tail-anchored, so the window still reaches the end: total grows with it.
    totalEventCountByAgent[agentId] = getFirstOffset(agentId) + eventsByAgent[agentId].length;
    bumpRenderVersion(agentId);
    m.redraw();
  } else if (didMerge) {
    bumpRenderVersion(agentId);
    m.redraw();
  }
}

/**
 * Prepend an older page to the window. When `offset` is given (the global index
 * of the page's first event, from the server) it becomes the window's new start;
 * otherwise the start is shifted back by the number of events actually added
 * (used by tests that prepend without a server round-trip).
 */
export function prependEvents(agentId: string, olderEvents: TranscriptEvent[], offset?: number, total?: number): void {
  const existing = eventsByAgent[agentId] ?? [];
  const byId = idMap(agentId);
  const deduped = olderEvents.filter((e) => !byId.has(e.event_id));
  if (deduped.length > 0) {
    for (const event of deduped) {
      byId.set(event.event_id, event);
    }
    eventsByAgent[agentId] = [...deduped, ...existing];
    firstOffsetByAgent[agentId] =
      offset !== undefined ? offset : Math.max(0, getFirstOffset(agentId) - deduped.length);
    if (total !== undefined) {
      totalEventCountByAgent[agentId] = total;
    }
    bumpRenderVersion(agentId);
    m.redraw();
  }
}

/** Append a newer page to the window (paging toward the tail from a window that
 *  was moved off the end by a jump). The window start is unchanged. */
export function appendForwardEvents(agentId: string, newerEvents: TranscriptEvent[], total?: number): void {
  const existing = eventsByAgent[agentId] ?? [];
  const byId = idMap(agentId);
  const deduped = newerEvents.filter((e) => !byId.has(e.event_id));
  if (deduped.length > 0) {
    for (const event of deduped) {
      byId.set(event.event_id, event);
    }
    eventsByAgent[agentId] = [...existing, ...deduped];
    if (total !== undefined) {
      totalEventCountByAgent[agentId] = total;
    }
    bumpRenderVersion(agentId);
    m.redraw();
  }
}

/**
 * Drop the oldest events beyond EVICT_TARGET_EVENTS to bound client memory.
 *
 * Returns the number of events removed (0 if under the cap). Callers should
 * only evict while the user is following the live tail, because removing
 * already-rendered older rows would shift a scrolled-up viewport. The window
 * start advances by the number removed, so the dropped history (still on the
 * server) is re-fetched via backfill on a later scroll-up.
 */
export function evictOldEvents(agentId: string): number {
  const existing = eventsByAgent[agentId];
  if (existing === undefined || existing.length <= MAX_HELD_EVENTS) {
    return 0;
  }
  const removeCount = existing.length - EVICT_TARGET_EVENTS;
  const removed = existing.slice(0, removeCount);
  const byId = idMap(agentId);
  for (const event of removed) {
    byId.delete(event.event_id);
  }
  eventsByAgent[agentId] = existing.slice(removeCount);
  firstOffsetByAgent[agentId] = getFirstOffset(agentId) + removeCount;
  bumpRenderVersion(agentId);
  return removeCount;
}

/** Replace the held window wholesale (initial load, or a jump to an offset). */
function resetEvents(agentId: string, events: TranscriptEvent[], offset: number, total: number): void {
  eventsByAgent[agentId] = events;
  eventByIdByAgent[agentId] = new Map(events.map((e) => [e.event_id, e]));
  firstOffsetByAgent[agentId] = offset;
  totalEventCountByAgent[agentId] = total;
  bumpRenderVersion(agentId);
}

function placeWindow(agentId: string, result: EventsResponse): void {
  const offset = result.offset ?? 0;
  const total = result.total ?? offset + result.events.length;
  resetEvents(agentId, result.events, offset, total);
  applyEnrichmentSnapshot(agentId, result.step_enrichment);
}

export async function fetchEvents(agentId: string): Promise<TranscriptEvent[]> {
  notFoundAgentIds.delete(agentId);

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId },
    });
    placeWindow(agentId, result);
    return result.events;
  } catch (error) {
    const requestError = error as { code?: number; message?: string };
    if (requestError.code === 404) {
      notFoundAgentIds.add(agentId);
    }
    throw error;
  }
}

/** Jump the window to an arbitrary global offset in one request (e.g. a scrollbar
 *  drag far from the loaded window), replacing the held events. */
export async function fetchWindowAtOffset(agentId: string, offset: number): Promise<void> {
  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId, offset: String(Math.max(0, offset)), limit: String(BACKFILL_PAGE_SIZE) },
    });
    placeWindow(agentId, result);
  } catch (error) {
    console.warn(`Failed to load events at offset ${offset} for agent ${agentId}`, error);
  }
}

export async function fetchBackfillEvents(agentId: string): Promise<void> {
  if (!hasMoreBefore(agentId)) {
    return;
  }
  const firstEventId = getFirstEventId(agentId);
  if (!firstEventId) {
    return;
  }

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId, before: firstEventId, limit: String(BACKFILL_PAGE_SIZE) },
    });
    if (result.events.length > 0) {
      prependEvents(agentId, result.events, result.offset, result.total);
    } else {
      // Nothing before the cursor: the window already starts at the beginning.
      firstOffsetByAgent[agentId] = 0;
      if (result.total !== undefined) {
        totalEventCountByAgent[agentId] = result.total;
      }
      bumpRenderVersion(agentId);
    }
  } catch (error) {
    // Backfill failure is non-fatal: the older history just isn't loaded, and
    // the window start is unchanged so the next scroll retries. Log it so a
    // persistent failure is diagnosable instead of vanishing silently.
    console.warn(`Failed to backfill older events for agent ${agentId}`, error);
  }
}

export async function fetchForwardEvents(agentId: string): Promise<void> {
  if (!hasMoreAfter(agentId)) {
    return;
  }
  const lastEventId = getLastEventId(agentId);
  if (!lastEventId) {
    return;
  }

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId, after: lastEventId, limit: String(BACKFILL_PAGE_SIZE) },
    });
    if (result.events.length > 0) {
      appendForwardEvents(agentId, result.events, result.total);
    } else if (result.total !== undefined) {
      // Nothing after the cursor: the window reaches the live tail.
      totalEventCountByAgent[agentId] = result.total;
      bumpRenderVersion(agentId);
    }
  } catch (error) {
    console.warn(`Failed to load newer events for agent ${agentId}`, error);
  }
}

export async function sendMessage(agentId: string, message: string): Promise<void> {
  if (!message.trim()) {
    return;
  }

  await m.request({
    method: "POST",
    url: apiUrl("/api/agents/:agentId/message"),
    params: { agentId },
    body: { message: message.trim() },
  });
}

export async function interruptAgent(agentId: string): Promise<void> {
  await m.request({
    method: "POST",
    url: apiUrl("/api/agents/:agentId/interrupt"),
    params: { agentId },
  });
}

// Compatibility shims
export class ConversationNotFoundError extends Error {
  constructor(agentId: string) {
    super(`Agent not found: ${agentId}`);
    this.name = "ConversationNotFoundError";
  }
}

export function getResponsesForConversation(_agentId: string): ResponseItem[] {
  return [];
}

export function getAllResponses(): Record<string, ResponseItem[]> {
  return {};
}

export function getLastResponseModel(_agentId: string): string | null {
  return null;
}

export function appendSyntheticResponse(): void {}

export async function insertResponseItem(): Promise<void> {}

export function fetchResponses(agentId: string): Promise<ResponseItem[]> {
  return fetchEvents(agentId).then(() => []);
}
