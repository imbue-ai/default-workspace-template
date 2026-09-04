/**
 * Event store for common transcript events.
 * Replaces the LLM response model with events fetched from session files.
 */

import m from "mithril";
import { apiUrl } from "../../base-path";
import { getActiveProjectId, getClientId, getDeviceKind } from "../../models/ClientIdentity";
import { noteBackendArrivals } from "./OutgoingMessages";
import { describeRequestError } from "../../models/request-error";

export interface SubagentMetadata {
  agent_type: string;
  description: string;
  session_id: string;
}

export interface ToolCall {
  tool_call_id: string;
  tool_name: string;
  // Size of the tool's raw input. The input itself never rides the event (the backend's
  // payload-free wire contract): expanding the row fetches it whole via the detail
  // endpoint; this only says whether there is anything to fetch.
  input_chars: number;
  // A tk lifecycle command, stamped whole so the step progress view reads titles and
  // close summaries without fetching the input. Absent for every other call.
  tk_command?: string;
  // Human labels, computed by the harness's own parser: the tool's identity for
  // the transcript block header, and verb + target for the live activity strip.
  // They differ for claude ("Tool: Read" / "Reading foo.py") and are usually equal
  // for codex, whose header would otherwise read a useless "Tool: exec". Rendering
  // these is why no view needs to know which harness produced the event. Optional
  // only for events parsed before the labels existed.
  header_label?: string;
  caption_label?: string;
  // For Agent tool calls: the description and subagent_type from the tool input, present
  // as soon as the call appears so the rich card can render before the subagent session is
  // linked. subagent_metadata (with the session_id for the click-through) is filled in once
  // the linkage is resolved.
  description?: string;
  subagent_type?: string;
  subagent_metadata?: SubagentMetadata;
  // The backend's render decision for this call: "hidden" (a tk lifecycle call --
  // a structural marker consumed by the step timeline, not work to render) or
  // "permission_request" (render the rich permission card). Absent = normal row.
  display?: "hidden" | "permission_request";
}

/**
 * Fields shared by every event, regardless of `type`. The `/events` stream is
 * the session transcript (user/assistant/tool_result); these are the only
 * transport-level fields guaranteed on all variants. tk step state (titles,
 * summaries) is carried in the transcript itself -- the lines tk prints on
 * stdout -- not in any side-channel.
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
  // The backend's render decision (harnesses/events.DisplayKind, stamped by every
  // harness's parser off the shared detector table): how this message renders.
  // Absent = the baseline user bubble. The raw harness markers (claude's isMeta /
  // isCompactSummary, sentinel tags) never reach the wire -- the decision does.
  display?: "hidden" | "chip" | "skill_expansion" | "permission_resolution";
  // Chip title ("Stop hook feedback", "Background task", ...) or skill name.
  display_label?: string;
  // The body to display when a wrapper sentinel was stripped (a fleet nudge).
  display_body?: string;
  // permission_resolution only: the verdict written onto the earlier card.
  resolution?: "granted" | "denied" | "error";
  // permission_resolution only: the resolved request's own id, when the notice
  // carries one (absent for a notice recorded before request-id embedding shipped,
  // which the walk instead correlates by arrival order -- see turn-grouping.ts).
  request_id?: string;
  // The activity path's signal that no model reply follows this message (model-bar
  // traffic, framework injections). Read by the backend's own activity derivation;
  // carried on the wire for completeness.
  non_turn_tail?: boolean;
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
  // True when the text is a model API error (e.g. "API Error: 529 Overloaded"),
  // stamped by the backend so the frontend can style it as an error. Harness-
  // agnostic: any harness that surfaces a provider error stamps these same fields.
  is_api_error: boolean;
  // The normalized error kind ("overloaded", "rate_limit", "api_error", ...), or
  // null when this is not an API error. Carried for wording; not required to render.
  api_error_kind: string | null;
  // True when the API error is the model provider's fault (a 5xx / overloaded)
  // rather than our request -- these get the "not Minds' fault" note.
  is_provider_fault: boolean;
  // True when the harness recorded READABLE reasoning for this turn (codex summaries,
  // pi thinking blocks, agy step reasoning; never claude, whose thinking is encrypted).
  // The text itself loads on demand through the detail endpoint.
  has_thinking?: boolean;
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
  // Size of the raw output. The output itself never rides the event (the backend's
  // payload-free wire contract): expanding the row fetches it whole via the detail
  // endpoint; this only says whether there is anything to fetch.
  output_chars: number;
  is_error: boolean;
  // A failed call's first output line, stamped resident so failures stay glanceable
  // without a fetch. Present only when is_error and the output had a line.
  error_snippet?: string;
  // The tk decoration lines the step progress view reads (Created/Updated/tk-step, plus
  // step-id echoes), stamped resident so the view never needs the raw output.
  tk_stamp?: string;
  // The permission request a latchkey creation POST echoed on stdout, parsed whole by
  // the backend off the full output; the permission card renders from this field.
  permission_request?: Record<string, unknown>;
  // NEVER on the wire: only the frontend-synthesized skill-expansion results (see
  // buildToolResultsWithSkillExpansions) carry inline output.
  output?: string;
}

/**
 * Every non-message marker a harness may emit. Mirrors the backend's
 * `SpecialEventKind` (harnesses/events.py); the two must be kept in step, which is what
 * turns an undeclared kind into a type error here rather than an event silently dropped.
 *
 * Turn boundaries come from codex, which records them in its rollout in real time.
 * Claude's transcript has no equivalent and emits none.
 */
export type SpecialEventKind = "turn_started" | "turn_completed" | "turn_aborted";

/**
 * A harness marker that is not a message. Nothing renders these -- they exist so the
 * event stream reflects the true transcript, and so the backend's activity derivation
 * has an authoritative signal. Declared here so the union stays exhaustive.
 */
export interface SpecialTranscriptEvent extends BaseTranscriptEvent {
  type: "special";
  kind: SpecialEventKind;
}

/**
 * A single entry in the transcript event stream, discriminated by `type`.
 * Narrow on `event.type` before touching variant-specific fields.
 *
 * The first three types are the core contract: every harness emits them with the same
 * fields, which is why no view needs to know which harness produced an event. `special`
 * is the declared extension point -- a harness may emit the kinds it registers, and
 * renderers ignore them.
 */
export type TranscriptEvent = UserMessageEvent | AssistantMessageEvent | ToolResultEvent | SpecialTranscriptEvent;

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
  // Global index of the first returned event within the full transcript, and the
  // transcript's total length. Together they place the loaded window in the whole
  // conversation: the client sizes the scrollbar for `total` and derives whether
  // more history exists above (offset > 0) and below (offset + events < total).
  offset?: number;
  total?: number;
}

// Hard cap on every transcript fetch. A request that never settles (e.g. a
// proxy holding the connection through a tunnel outage) would otherwise pin
// the panel's single-fetch-at-a-time guard forever, freezing all paging until
// a full page reload. Matches the forwarding proxy's own 30s timeout.
const EVENTS_REQUEST_TIMEOUT_MS = 30_000;

/** m.request config hook applying the transcript-fetch timeout. */
function applyEventsRequestTimeout(xhr: XMLHttpRequest): XMLHttpRequest {
  xhr.timeout = EVENTS_REQUEST_TIMEOUT_MS;
  return xhr;
}

// Client-side memory for a transcript is bounded by the scroll engine's fill
// planner (see models/transcriptScroll/fillPlanner), which drives loading up to
// its physical cap and issues explicit evictions beyond it; the store itself
// imposes no cap.

// All per-agent transcript state is owned by one TranscriptStore instance per
// agent (see storeByAgent below). The held events are a single contiguous window
// of the full transcript: `firstOffset` is the global index of events[0] and
// `total` the full length; whether more history exists above/below and the
// scrollbar size are derived from those two. The window can sit anywhere (the live
// tail is just the case where it ends at `total`), so it pages in both directions
// and can be replaced wholesale by a jump to an arbitrary offset.
//
// `renderVersion` is a monotonic counter the chat view memoizes its (expensive)
// turn-grouping on, so a scroll-only redraw -- which changes no data -- reuses the
// cached rows instead of re-walking the whole held transcript every frame (the
// dominant scroll cost on a long conversation). Its invariant: it must bump on
// every mutation that changes what renders and never on a no-op. The fields are
// #private and the only writer of renderVersion is the private #commit funnel, so
// no mutation -- here or in a future method -- can change the store without going
// through the one place the version is bumped. The store is a pure data holder: a
// mutation returns whether anything changed so the module-level wrappers can decide
// redraws, but the store never touches the view layer itself.
class TranscriptStore {
  #events: TranscriptEvent[] = [];
  // event_id -> stored event, mirroring #events: O(1) dedup on append/prepend and
  // O(1) lookup so a re-broadcast can upgrade an event in place (see append).
  #byId = new Map<string, TranscriptEvent>();
  #firstOffset = 0;
  // Total events in the full server-side transcript (see EventsResponse.total).
  #total = 0;
  #renderVersion = 0;

  get events(): TranscriptEvent[] {
    return this.#events;
  }

  get eventCount(): number {
    return this.#events.length;
  }

  get firstOffset(): number {
    return this.#firstOffset;
  }

  /** Total events in the full transcript, for scrollbar sizing. Never less than
   *  the loaded window's end, so the window always fits inside it. */
  get total(): number {
    return Math.max(this.#total, this.#firstOffset + this.#events.length);
  }

  get renderVersion(): number {
    return this.#renderVersion;
  }

  /** Older history exists before the window (it doesn't start at 0). */
  get hasMoreBefore(): boolean {
    return this.#firstOffset > 0;
  }

  /** Newer history exists after the window (it doesn't reach the live tail) --
   *  true only after a jump/scroll moved the window off the end. */
  get hasMoreAfter(): boolean {
    return this.#firstOffset + this.#events.length < this.total;
  }

  get firstEventId(): string | null {
    return this.#events.length > 0 ? this.#events[0].event_id : null;
  }

  get lastEventId(): string | null {
    return this.#events.length > 0 ? this.#events[this.#events.length - 1].event_id : null;
  }

  /**
   * The single mutation funnel and the ONLY writer of #renderVersion. Each mutator
   * expresses its change as `mutate` and returns whether anything that renders
   * changed; the version bumps iff it did, and #commit returns that flag so the
   * caller can skip a redraw on a no-op. Private alongside the #private fields, this
   * is what makes the bump impossible to forget -- there is no other way to change
   * the store.
   */
  #commit(mutate: () => boolean): boolean {
    const changed = mutate();
    if (changed) {
      this.#renderVersion += 1;
    }
    return changed;
  }

  /**
   * Append live tail events. They only belong in the window when it is
   * tail-anchored (reaches the live end); if the user has jumped to an earlier
   * position, appending would break contiguity, so brand-new events are dropped
   * (re-fetched via forward paging on return to the tail). A late re-broadcast that
   * upgrades an already-held event in place is applied regardless of position.
   */
  append(newEvents: TranscriptEvent[]): boolean {
    const tailAnchored = !this.hasMoreAfter;
    return this.#commit(() => {
      let added = false;
      let merged = false;
      for (const event of newEvents) {
        const prior = this.#byId.get(event.event_id);
        if (prior === undefined) {
          if (tailAnchored) {
            this.#events.push(event);
            this.#byId.set(event.event_id, event);
            added = true;
          }
        } else if (mergeLateSubagentMetadata(prior, event)) {
          // The narrow claude case: late subagent linkage merges onto the held call.
          merged = true;
        } else if (JSON.stringify(prior) !== JSON.stringify(event)) {
          // A general supersession: the backend re-broadcast an already-held event with
          // updated content (codex re-serialises an event under the same id). Replace it
          // in place -- position unchanged -- so the held view reflects the latest.
          const index = this.#events.indexOf(prior);
          if (index !== -1) {
            this.#events[index] = event;
            this.#byId.set(event.event_id, event);
            merged = true;
          }
        }
      }
      if (added) {
        // Tail-anchored, so the window still reaches the end: total grows with it.
        this.#total = this.#firstOffset + this.#events.length;
      }
      return added || merged;
    });
  }

  /**
   * Prepend an older page. When `offset` is given (the global index of the page's
   * first event, from the server) it becomes the window's new start; otherwise the
   * start shifts back by the number of events added (used by tests that prepend
   * without a server round-trip).
   */
  prepend(olderEvents: TranscriptEvent[], offset?: number, total?: number): boolean {
    return this.#commit(() => {
      // Contiguity guard: a server page carries the global index of its first
      // event, and a *current* backfill response always ends exactly where the
      // window starts (it was fetched with before=<window's first event>). A
      // page that does not reach the window start is stale -- issued against a
      // window that has since been replaced (e.g. a reconnect snapshot landed
      // while it was in flight). Gluing it on would corrupt the window's
      // offset arithmetic permanently, so discard it instead.
      if (offset !== undefined && (offset > this.#firstOffset || offset + olderEvents.length < this.#firstOffset)) {
        return false;
      }
      const deduped = olderEvents.filter((e) => !this.#byId.has(e.event_id));
      if (deduped.length === 0) {
        return false;
      }
      for (const event of deduped) {
        this.#byId.set(event.event_id, event);
      }
      this.#events = [...deduped, ...this.#events];
      this.#firstOffset = offset !== undefined ? offset : Math.max(0, this.#firstOffset - deduped.length);
      if (total !== undefined) {
        this.#total = total;
      }
      return true;
    });
  }

  /** Append a newer page (paging toward the tail from a window moved off the end
   *  by a jump). The window start is unchanged. */
  appendForward(newerEvents: TranscriptEvent[], total?: number): boolean {
    return this.#commit(() => {
      const deduped = newerEvents.filter((e) => !this.#byId.has(e.event_id));
      if (deduped.length === 0) {
        return false;
      }
      for (const event of deduped) {
        this.#byId.set(event.event_id, event);
      }
      this.#events = [...this.#events, ...deduped];
      if (total !== undefined) {
        this.#total = total;
      }
      return true;
    });
  }

  /**
   * Drop `count` events from one end of the window (the scroll engine's fill
   * planner decides which side and how many). Evicting older events advances the
   * window start, so the dropped history (still on the server) reads as
   * backfillable again; evicting newer events pulls the window off the live tail,
   * so it reads as forward-pageable.
   */
  evict(side: "older" | "newer", count: number): number {
    let removeCount = 0;
    this.#commit(() => {
      removeCount = Math.min(Math.max(0, count), this.#events.length);
      if (removeCount === 0) {
        return false;
      }
      const removed =
        side === "older" ? this.#events.slice(0, removeCount) : this.#events.slice(this.#events.length - removeCount);
      for (const event of removed) {
        this.#byId.delete(event.event_id);
      }
      if (side === "older") {
        this.#events = this.#events.slice(removeCount);
        this.#firstOffset += removeCount;
      } else {
        this.#events = this.#events.slice(0, this.#events.length - removeCount);
      }
      return true;
    });
    return removeCount;
  }

  /** Replace the held window wholesale (initial load, or a jump to an offset). */
  reset(events: TranscriptEvent[], offset: number, total: number): void {
    this.#commit(() => {
      this.#events = events;
      this.#byId = new Map(events.map((e) => [e.event_id, e]));
      this.#firstOffset = offset;
      this.#total = total;
      return true;
    });
  }

  /** An older page came back empty: the window already starts at the beginning. */
  markReachedStart(total?: number): void {
    this.#commit(() => {
      this.#firstOffset = 0;
      if (total !== undefined) {
        this.#total = total;
      }
      return true;
    });
  }

  /** A newer page came back empty: the window reaches the live tail; reconcile the
   *  total the server now reports. */
  reconcileTotalAtTail(total: number): void {
    this.#commit(() => {
      this.#total = total;
      // The server just said "nothing exists after your last event": the held
      // window IS the live tail. If the bookkeeping still thinks the window
      // falls short of `total` (events were missed during an outage, so the
      // held count undercounts the range), shift the believed window start so
      // the end lines up with the tail. The discrepancy moves above the
      // window, where backfill can genuinely re-fetch it. Without this,
      // hasMoreAfter sticks true forever: forward paging refires with no
      // possible progress and append() drops every future live event.
      this.#firstOffset = Math.max(0, total - this.#events.length);
      return true;
    });
  }
}

const storeByAgent: Record<string, TranscriptStore> = {};
const notFoundAgentIds = new Set<string>();

/** Where an agent's transcript snapshot stands: in flight, failed, or settled. */
export interface TranscriptLoadState {
  readonly phase: "idle" | "loading" | "error";
  /** Why it failed. Set when `phase` is "error", null otherwise. */
  readonly error: string | null;
}

const IDLE_LOAD_STATE: TranscriptLoadState = { phase: "idle", error: null };

// Where each agent's snapshot load stands. It lives here rather than in the
// panel because every path that reloads a transcript -- the panel's own load,
// the tab's Refresh, and the stream's background reconnect -- goes through
// `fetchEvents`, and only one of those is the panel. A panel holding its own
// copy could not be cleared by the other two, so a recovered transcript stayed
// hidden behind a stale error until the page was reloaded. Holding the whole
// phase rather than just the error keeps the in-flight state visible to those
// same three paths, so a reload nobody started still reads as loading.
const loadStateByAgent = new Map<string, TranscriptLoadState>();

// Which snapshot attempt an agent's state belongs to. Those same three paths can
// have two fetches outstanding at once, and they settle in whatever order the
// network allows: a request hung on a dead tunnel settles up to
// EVENTS_REQUEST_TIMEOUT_MS after a later one has already landed. Only the newest
// attempt speaks for the agent, so an older one's failure cannot put the panel
// back on an error screen for a transcript that has since loaded. Same staleness
// fence the paging fetches below apply to their window.
let loadAttemptCounter = 0;
const newestLoadAttemptByAgent = new Map<string, number>();

/** Whether this attempt is still the agent's newest, i.e. whether its outcome still counts. */
function isNewestLoadAttempt(agentId: string, attempt: number): boolean {
  return newestLoadAttemptByAgent.get(agentId) === attempt;
}

function storeFor(agentId: string): TranscriptStore {
  let store = storeByAgent[agentId];
  if (store === undefined) {
    store = new TranscriptStore();
    storeByAgent[agentId] = store;
  }
  return store;
}

// Read accessors. These never create a store, so an unknown agent reads as empty
// defaults rather than allocating one on a mere read.
export function getRenderVersion(agentId: string): number {
  return storeByAgent[agentId]?.renderVersion ?? 0;
}

export function getFirstOffset(agentId: string): number {
  return storeByAgent[agentId]?.firstOffset ?? 0;
}

export function getTotalEventCount(agentId: string): number {
  return storeByAgent[agentId]?.total ?? 0;
}

export function hasMoreBefore(agentId: string): boolean {
  return storeByAgent[agentId]?.hasMoreBefore ?? false;
}

export function hasMoreAfter(agentId: string): boolean {
  return storeByAgent[agentId]?.hasMoreAfter ?? false;
}

export function isConversationNotFound(agentId: string): boolean {
  return notFoundAgentIds.has(agentId);
}

/** Where this agent's transcript snapshot load stands; "idle" for one never attempted. */
export function getConversationLoadState(agentId: string): TranscriptLoadState {
  return loadStateByAgent.get(agentId) ?? IDLE_LOAD_STATE;
}

export function getEventsForAgent(agentId: string): TranscriptEvent[] {
  return storeByAgent[agentId]?.events ?? [];
}

export function getEventCount(agentId: string): number {
  return storeByAgent[agentId]?.eventCount ?? 0;
}

export function getFirstEventId(agentId: string): string | null {
  return storeByAgent[agentId]?.firstEventId ?? null;
}

export function getLastEventId(agentId: string): string | null {
  return storeByAgent[agentId]?.lastEventId ?? null;
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
  if (storeFor(agentId).append(newEvents)) {
    m.redraw();
  }
  // Route live user-message arrivals through the optimistic-send layer so a
  // "Sending…" bubble drops the instant its real message lands in the transcript
  // (no overlap). Deduped by event_id in noteBackendArrivals, so a re-streamed
  // event is harmless. Only the live tail feeds this -- paging/backfill of old
  // history goes through the other append paths and must not drop live bubbles.
  const userEventIds = newEvents.filter((event) => event.type === "user_message").map((event) => event.event_id);
  if (userEventIds.length > 0) {
    noteBackendArrivals(agentId, userEventIds);
  }
}

export function prependEvents(agentId: string, olderEvents: TranscriptEvent[], offset?: number, total?: number): void {
  if (storeFor(agentId).prepend(olderEvents, offset, total)) {
    m.redraw();
  }
}

export function appendForwardEvents(agentId: string, newerEvents: TranscriptEvent[], total?: number): void {
  if (storeFor(agentId).appendForward(newerEvents, total)) {
    m.redraw();
  }
}

export function evictEvents(agentId: string, side: "older" | "newer", count: number): number {
  const removed = storeFor(agentId).evict(side, count);
  if (removed > 0) {
    m.redraw();
  }
  return removed;
}

function placeWindow(agentId: string, result: EventsResponse): void {
  const offset = result.offset ?? 0;
  const total = result.total ?? offset + result.events.length;
  const store = storeFor(agentId);
  store.reset(result.events, offset, total);
}

export async function fetchEvents(agentId: string): Promise<TranscriptEvent[]> {
  notFoundAgentIds.delete(agentId);
  // Moved on the attempt, not on its outcome: whoever is about to learn the
  // outcome must not be shown the previous one. Only the snapshot tracks this --
  // a failed page or jump below leaves the loaded window intact and is
  // deliberately non-fatal, so it must not blank a readable transcript. Starting
  // an attempt always supersedes any outstanding one, so this write needs no
  // fence; only the outcomes below do.
  const attempt = ++loadAttemptCounter;
  newestLoadAttemptByAgent.set(agentId, attempt);
  loadStateByAgent.set(agentId, { phase: "loading", error: null });

  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId },
      config: applyEventsRequestTimeout,
    });
    // Fenced for the same reason the outcome writes below are, and it matters
    // more here: a superseded attempt can still *succeed*, just late, and
    // `placeWindow` replaces the window wholesale. Letting an older snapshot land
    // on top of a newer one reverts the transcript and drops whatever the stream
    // appended in between, with no way back -- placeWindow also resets
    // firstOffset and hasMoreAfter, so neither backfill nor forward paging can
    // reach the lost events again.
    if (!isNewestLoadAttempt(agentId, attempt)) {
      return result.events;
    }
    placeWindow(agentId, result);
    loadStateByAgent.set(agentId, IDLE_LOAD_STATE);
    return result.events;
  } catch (error) {
    // The not-found latch is fenced alongside the state because the panel acts on
    // it harder: it renders "No conversation data" ahead of (and unlike) the load
    // state, ungated by whether a transcript is already on screen, and disconnects
    // the stream. A superseded attempt's 404 would blank a live chat.
    if (isNewestLoadAttempt(agentId, attempt)) {
      const requestError = error as { code?: number; message?: string };
      if (requestError.code === 404) {
        notFoundAgentIds.add(agentId);
      }
      loadStateByAgent.set(agentId, { phase: "error", error: describeRequestError(error) });
    }
    throw error;
  }
}

/** Jump the window to an arbitrary global offset in one request (e.g. a scrollbar
 *  drag far from the loaded window), replacing the held events. */
export async function fetchWindowAtOffset(agentId: string, offset: number, limit: number): Promise<void> {
  try {
    const result = await m.request<EventsResponse>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events"),
      params: { agentId, offset: String(Math.max(0, offset)), limit: String(limit) },
      config: applyEventsRequestTimeout,
    });
    placeWindow(agentId, result);
  } catch (error) {
    console.warn(`Failed to load events at offset ${offset} for agent ${agentId}`, error);
  }
}

export async function fetchBackfillEvents(agentId: string, limit: number): Promise<void> {
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
      params: { agentId, before: firstEventId, limit: String(limit) },
      config: applyEventsRequestTimeout,
    });
    // Staleness fence: if the window changed while this page was in flight
    // (a reconnect snapshot or a jump replaced it), the response was issued
    // against coordinates that no longer exist -- applying it would corrupt
    // the window arithmetic. Discard; the next scroll retries with a
    // current cursor.
    if (getFirstEventId(agentId) !== firstEventId) {
      console.warn(`[si-transcript] discarding stale backfill page for agent ${agentId} (window changed)`);
      return;
    }
    if (result.events.length > 0) {
      prependEvents(agentId, result.events, result.offset, result.total);
    } else {
      // Nothing before the cursor: the window already starts at the beginning.
      storeFor(agentId).markReachedStart(result.total);
    }
  } catch (error) {
    // Backfill failure is non-fatal: the older history just isn't loaded, and
    // the window start is unchanged so the next scroll retries. Log it so a
    // persistent failure is diagnosable instead of vanishing silently.
    console.warn(`Failed to backfill older events for agent ${agentId}`, error);
  }
}

export async function fetchForwardEvents(agentId: string, limit: number): Promise<void> {
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
      params: { agentId, after: lastEventId, limit: String(limit) },
      config: applyEventsRequestTimeout,
    });
    // Staleness fence, mirroring fetchBackfillEvents: discard the page if the
    // window's tail moved while it was in flight (live append or a snapshot
    // reset) -- the next maybePage refires against the current cursor.
    if (getLastEventId(agentId) !== lastEventId) {
      console.warn(`[si-transcript] discarding stale forward page for agent ${agentId} (window changed)`);
      return;
    }
    if (result.events.length > 0) {
      appendForwardEvents(agentId, result.events, result.total);
    } else if (result.total !== undefined) {
      // Nothing after the cursor: the window reaches the live tail.
      storeFor(agentId).reconcileTotalAtTail(result.total);
    }
  } catch (error) {
    console.warn(`Failed to load newer events for agent ${agentId}`, error);
  }
}

/** The full deferred payloads of one event, fetched on demand from the detail endpoint. */
export interface EventDetail {
  inputs_by_tool_call_id: Record<string, string>;
  output: string | null;
  thinking: string | null;
}

export type EventDetailState =
  | { state: "loading" }
  | { state: "loaded"; detail: EventDetail }
  // The source line is gone (the transcript was rewritten/cleaned up); render a quiet
  // "payload no longer available" placeholder.
  | { state: "unavailable" };

// Frontend-only payload cache, per agent, for the page session: the backend serves detail
// reads statelessly and never caches them, so whatever the user expanded is remembered
// here (alongside expansion-state) and survives virtualization remounts without refetching.
const detailByAgent = new Map<string, Map<string, EventDetailState>>();
// Bumped on every detail-state change, per agent, so memoized message wrappers know to
// repaint an expanded block whose payload just arrived.
const detailVersionByAgent = new Map<string, number>();
// How long a transiently-failed detail fetch blocks its retry (the failed entry stays in
// "loading" until then), pacing the expanded row's heal-on-render re-request.
const DETAIL_RETRY_DELAY_MS = 3000;

export function getEventDetailState(agentId: string, eventId: string): EventDetailState | undefined {
  return detailByAgent.get(agentId)?.get(eventId);
}

export function getEventDetailVersion(agentId: string): number {
  return detailVersionByAgent.get(agentId) ?? 0;
}

function bumpDetailVersion(agentId: string): void {
  detailVersionByAgent.set(agentId, getEventDetailVersion(agentId) + 1);
}

/** Kick off a detail fetch if none is cached or in flight. Idempotent; redraws on arrival. */
export function requestEventDetail(agentId: string, eventId: string): void {
  let byEvent = detailByAgent.get(agentId);
  if (byEvent === undefined) {
    byEvent = new Map<string, EventDetailState>();
    detailByAgent.set(agentId, byEvent);
  }
  if (byEvent.has(eventId)) {
    return;
  }
  byEvent.set(eventId, { state: "loading" });
  void m
    .request<EventDetail>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/events/:eventId/detail"),
      params: { agentId, eventId },
      config: applyEventsRequestTimeout,
    })
    .then((detail) => {
      byEvent.set(eventId, { state: "loaded", detail });
      bumpDetailVersion(agentId);
      m.redraw();
    })
    .catch((error: { code?: number }) => {
      if (error.code === 404) {
        byEvent.set(eventId, { state: "unavailable" });
        bumpDetailVersion(agentId);
        m.redraw();
        return;
      }
      // Transient failure: drop the entry so a still-expanded row retries -- but only
      // after a delay. Dropping immediately would let the expanded render's healing
      // re-request turn a persistent failure (backend restarting) into a tight
      // fetch loop; holding the "loading" entry blocks re-requests until the timer.
      setTimeout(() => {
        if (byEvent.get(eventId)?.state === "loading") {
          byEvent.delete(eventId);
          bumpDetailVersion(agentId);
          m.redraw();
        }
      }, DETAIL_RETRY_DELAY_MS);
    });
}

/** Mint a stable per-message id at send time (contract A4). The backend keys its
 *  'Sending' record on it so an interrupt can reconcile the message per id and
 *  return it to the composer if it never committed. Returned to the caller so a
 *  later optimistic "Sending..." paint can carry the same id. */
export function mintMessageId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `msg-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

// Subscribers told when the user submits a message for an agent. The scroll
// engine snaps back to following the tail on send (MESSAGE_SENT transition);
// routing the signal through here covers every send path without the composer
// knowing about scrolling.
const messageSentListeners = new Set<(agentId: string) => void>();

export function addMessageSentListener(listener: (agentId: string) => void): void {
  messageSentListeners.add(listener);
}

export function removeMessageSentListener(listener: (agentId: string) => void): void {
  messageSentListeners.delete(listener);
}

export async function sendMessage(agentId: string, message: string, messageId?: string): Promise<string> {
  const trimmed = message.trim();
  const id = messageId ?? mintMessageId();
  if (!trimmed) {
    return id;
  }
  for (const listener of messageSentListeners) {
    listener(agentId);
  }

  // The client identity rides along so the server can record which browser
  // (and which named layout) the message came from -- that is how agents
  // attribute a request to a client via `layout.py context`. The message_id is
  // the stable send-time id the backend reconciles delivery against (A4).
  await m.request({
    method: "POST",
    url: apiUrl("/api/agents/:agentId/message"),
    params: { agentId },
    body: {
      message: trimmed,
      message_id: id,
      client_id: getClientId(),
      active_layout: getActiveProjectId(),
      device_kind: getDeviceKind(),
    },
  });
  // Bump this chat's OOM recency now that a message was accepted, so an actively
  // messaged chat is more protected from a memory shed than idler ones.
  return id;
}

export async function interruptAgent(agentId: string): Promise<void> {
  await m.request({
    method: "POST",
    url: apiUrl("/api/agents/:agentId/interrupt"),
    params: { agentId },
  });
}

/** Shoulder tap: deliver the queued messages to the agent now. ONE harness-agnostic call --
 *  the backend dispatches per harness (pi inbox sentinel, codex ledger gate, claude cancel
 *  chord) behind this single endpoint, so the frontend never branches on harness. Fire-and-
 *  forget: the next WS snapshot (empty group) plus the committed turn reflect the result and
 *  nothing is painted locally. Whether the tap is available at all is the backend's
 *  ``shoulder_tap_available`` flag, which greys the button -- so this is never called when the
 *  backend would refuse it, and a benign no-op status is returned rather than an error. */
export async function shoulderTap(agentId: string): Promise<{ status: string; block: string }> {
  const result = await m.request<{ status: string; block?: string }>({
    method: "POST",
    url: apiUrl("/api/agents/:agentId/shoulder-tap-atomic"),
    params: { agentId },
  });
  // ``block`` is non-empty only when a native (codex) tap's combined resend failed to submit: the
  // parked text is handed back for the composer so it is never swallowed (contract A1a). Default to
  // "" for the harnesses/paths that never return one.
  return { status: result.status, block: result.block ?? "" };
}

/** Interrupt to composer: restart the agent and get the queued messages back as
 *  one concatenated block to drop into the composer, unsent. */
export async function drainToComposer(agentId: string): Promise<{ block: string }> {
  return await m.request<{ block: string }>({
    method: "POST",
    url: apiUrl("/api/agents/:agentId/drain-to-composer"),
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
