/**
 * Step grouping: a single in-order walk of the transcript.
 *
 * The progress view is a pure frontend for the agent session transcript.
 * BOTH structure and decoration come from the transcript -- there is no
 * side-channel.
 *
 * - *Structure* (which steps exist, their order, their open/close transitions,
 *   which events belong to which step) is read from transcript position. tk
 *   lifecycle commands (`tk create/start/close`) appear as Bash tool calls;
 *   their results carry the canonical id and status (`Updated <id> -> <status>`),
 *   which is all the structure we need.
 * - *Decoration* (titles, close summaries) is read from the machine-readable
 *   lines tk prints on stdout: `Created <id>: <title>` (create),
 *   `tk-step <id> title: <title>` (start/close), and
 *   `tk-step <id> summary: <summary>` (close). These are parsed in one global
 *   pass over all events into a per-id decoration map, so a node resolves its
 *   title from a `tk start`/create line far earlier in the transcript.
 * - For *historical* transcripts that predate those output lines, a best-effort
 *   input-preview fallback recovers titles/summaries from the tk command inputs
 *   (`tk create --step "Title"`, `tk close <id> "summary"`). It is allowed to be
 *   imperfect; it serves a frozen corpus only.
 *
 * The conversation renders in transcript order: user messages, the agent's
 * replies and work, chips, cards. Steps render apart from it, on a timeline.
 * The first step that appears (created, started or closed) while no step is
 * open starts a timeline, and every later step joins it until each of them is
 * done. While a step on it is not done, the timeline is live: it renders after
 * everything else in the conversation, so each user message and agent reply
 * that arrives meanwhile renders above it. Finished steps stay on it, with
 * their summaries. When its last step is done it freezes at that point in the
 * conversation, and what follows (usually the agent's wrap-up reply) renders
 * below it. The next step after that starts a new timeline. A step never
 * renders twice.
 *
 * The walk maintains a single "current open step": events while a step is open
 * group under it and render inside its node; events while none is open render
 * inline in the conversation. When a step closes, any prose it spoke after its
 * last work is ejected into the conversation at its transcript position, so a
 * closing remark is promoted out of the step rather than buried in it.
 *
 * A system chip or notice (Stop-hook feedback, a browser-fleet nudge, a finished
 * background task, a bash-mode command and its output) heads a section of the
 * conversation, as a user message does. The one exception is a chip landing
 * inside an open handoff node, which stays in the handoff's section.
 *
 * One message is never grouped under a step: an agent permission request, or a
 * secret request (the same shape, answered on the card itself). It
 * is lifted out into a dedicated inline break (the `permission` timeline item)
 * so the user always sees it and can respond without expanding a step. The step
 * it interrupted stays open, so work resumed afterwards keeps grouping under it.
 * When the user later grants or denies the request, the app injects a plain
 * notification user message. The walk reads its verdict onto the request's card
 * and opens a new section with no bubble: the raw text isn't shown (its verdict
 * is on the card). The notification carries the
 * resolved request's own id (see message-classification's resolutionRequestIdOf),
 * so a card looks up its verdict by that id rather than by arrival order: two
 * requests resolved out of the order they were created never swap their
 * cards' verdicts, and a message that batches more than one permission request
 * resolves each of its cards independently. A notification with no id (recorded
 * before Imbue Studio embedded ids) attributes nothing here -- an embedded page
 * recovers such verdicts from the response log via the card's hydration query.
 *
 * A compaction ("Context was compacted") breaks nothing. It joins the inline
 * conversation at its transcript position, where its chips share a chip row with the
 * tool calls beside it, or follows the wrap-up reply when it landed after one
 * (`trailing_status`). While the agent compacts, the compaction running now is
 * placed the same way, at the end of the transcript, until its event lands.
 *
 * This module reads no timestamps. Pending placeholders are ordered by
 * transcript position; grouping and the positioning of any transitioned step
 * read transcript order alone.
 */

import type {
  TranscriptEvent,
  AgentSwitchEvent,
  AssistantMessageEvent,
  UserMessageEvent,
  ToolResultEvent,
  ToolCall,
  CompactionCause,
} from "../models/Response";
import type { HandoffState } from "../models/Chats";
import { landedCompaction, runningCompaction, type CompactionPart } from "./compaction-chips";
import type { RunPart } from "./message-renderers";
import { COMPACTING_STATE } from "../models/activityState";
import { SEED_HARNESS } from "../models/Response";
import { isHandoffPromptChip } from "../models/handoffPrompt";
import type { RequestResolution } from "./message-classification";
import { isFiledPermissionRequest } from "./permission-card";
import { isFiledSecretRequest, parseSecretRequest } from "./secret-card";
import {
  isHandoffSummaryRequest,
  isNonBoundaryUserMessage,
  isNoticeUserMessage,
  isPromptWithContext,
  isStatusUserMessage,
  isSystemChipUserMessage,
  resolutionOf,
  resolutionRequestIdOf,
  secretResolutionNoteOf,
  secretResolutionOf,
  secretResolutionRequestIdOf,
} from "./message-classification";

export type StepStatus = "pending" | "active" | "done";

/** A step as it should render on one timeline. A step started again after its
 *  timeline froze gets a second, independent node on the next timeline. */
export interface StepNode {
  ticket_id: string;
  title: string;
  status: StepStatus;
  /** Close summary, shown when done. */
  summary: string | null;
  /** Latest in-step prose, shown as the live caption under the step. For a
   *  non-frontier step this is the latest prose followed by more work in the
   *  same step (trailing closing prose having been ejected); for the live
   *  frontier step it is the last thing the agent said, so a just-spoken line
   *  shows even before the next tool call. */
  narration: string | null;
  /** True when this is the live step the agent is currently on -- the only one
   *  that may show a spinner. False once settled (idle, or superseded by a
   *  later step). */
  is_frontier: boolean;
  /** The grouped real-work events (assistant text + non-tk tool calls) that
   *  occurred while this step was the open step. */
  events: AssistantMessageEvent[];
}

/** The handoff between two agents of one chat, as one node: the chat app's summary request, the
 *  retiring agent's work in answer (the summary write), and the switch that closed it. Open while
 *  the switch has not landed: a handoff still running, one that failed, or one called off. */
export interface HandoffNode {
  key: string;
  /** The summary request that opened the node; null for a node the switch alone made: the
   *  loaded window starts after the request, or a fresh start asked for no summary. */
  request: UserMessageEvent | null;
  /** The retiring agent's turn after the request: what expanding the node shows. */
  events: AssistantMessageEvent[];
  /** The switch that completed the handoff, once it has. */
  switch: AgentSwitchEvent | null;
  /** The prompt the successor was started with (the chip carrying the summary and the user's message),
   *  once it has reached the successor's segment: shown inside the node rather than as a chip of its own. */
  prompt: UserMessageEvent | null;
}

/** Whether the node's switch was a fresh start, which had no handoff to show. */
export function isFreshStartNode(node: HandoffNode): boolean {
  return node.switch !== null && node.switch.is_fresh_start;
}

/** One item of a section, in transcript order. */
export type TimelineItem =
  | { kind: "step"; step: StepNode }
  /** Real work and/or prose with no step open, or a step's ejected closing
   *  prose, and any compaction among it. Rendered inline as plain chat. */
  | { kind: "ungrouped"; key: string; events: RunPart[] }
  /** An agent permission request, lifted out of any open step so it always
   *  renders inline as a thread-breaking block. The user must be able to see and
   *  act on it without expanding a step. `event` may carry more than one
   *  permission-request tool call (a batched request); each card resolves its
   *  own verdict by looking up its own request id in `resolutionsByRequestId`
   *  (shared across the whole transcript, built once in buildSections), which
   *  has no entry while the request still awaits a decision. */
  | {
      kind: "permission";
      event: AssistantMessageEvent;
      resolutionsByRequestId: ReadonlyMap<string, RequestResolution>;
      /** The user's note on a declined secret request, by request id (shared like the verdicts). */
      secretNotesByRequestId: ReadonlyMap<string, string>;
    }
  /** A system chip or notice that landed inside an open handoff node, shown inline after it.
   *  Anywhere else a chip heads a section of its own (SectionView.user_event). */
  | { kind: "chip"; event: UserMessageEvent }
  /** The chat's handoff to another agent, at the point its summary was asked for (or, with no
   *  request in the window, at the switch itself, when its prompt reached it). */
  | { kind: "handoff"; node: HandoffNode };

/** One block of the rendered conversation: either a timeline (only `step` items, no
 *  user message and no reply) or a section of the conversation (a user message and what
 *  followed it, with no `step` items). */
export interface SectionView {
  /** The user message that opened this section -- a human turn, or a system chip
   *  or notice the agent resumed after -- or null for a timeline, and for content
   *  that precedes the first user message or follows a frozen timeline, a
   *  permission or secret verdict, or an agent switch that carries no message. */
  user_event: UserMessageEvent | null;
  key: string;
  items: TimelineItem[];
  /** The final run of ungrouped prose: the section's reply, rendered after its items. */
  trailing_reply: AssistantMessageEvent[];
  /** Compactions that landed after the trailing reply, rendered after it. */
  trailing_status: CompactionPart[];
}

/** A status transition line printed by tk on every state change:
 *  `Updated <id> -> <status>` (see system/vendor/tk/ticket). Global so a batched
 *  command that flips several tickets is read in order. */
const TK_UPDATED_RE = /^Updated (\S+) -> (open|in_progress|closed)\r?$/gm;

/** A step id (minted by `tk create --step`) carries a literal `-step-` segment,
 *  e.g. `cod-step-f1zl`; a regular ticket id has none (`cod-f1zl`). The walk
 *  uses this to recognise a step from its transition id alone, so a regular
 *  ticket the agent picked up (which prints the same `Updated <id> -> <status>`
 *  line) is never minted as a phantom timeline node. */
const STEP_ID_RE = /-step-[a-z0-9]+$/;

/** Decoration output lines tk prints on stdout. The format is defined in
 *  `system/vendor/tk/ticket` (cmd_create/start/close) and is also protected from
 *  truncation by the backend parser (`session_parser.py`); keep all three in
 *  sync. The id is required to carry the `-step-` segment so a stray
 *  "Created ..." line in other tool output (e.g. a scaffolder's "Created lib
 *  at ...") is never mistaken for a step. */
const CREATED_RE = /^Created (\S+-step-[a-z0-9]+): (.*)$/gm;
const TK_STEP_TITLE_RE = /^tk-step (\S+) title: (.*)$/gm;
const TK_STEP_SUMMARY_RE = /^tk-step (\S+) summary: (.*)$/gm;

/** Historical input fallback: titles from `tk create --step "Title"` and
 *  summaries from `tk close <id> "summary"`, pulled from the (un-truncated) tk
 *  command. `\b` anchors the verb to a word boundary so a tk mention inside
 *  another token is not matched. Both single- and double-quoted arguments are
 *  accepted (agents use both); the title/summary is whichever group matched. */
const CREATE_TITLE_RE = /\b(?:tk|ticket)\s+(?:super\s+)?create\b[^"']*?(?:"([^"]*)"|'([^']*)')/g;
const CLOSE_SUMMARY_RE = /\b(?:tk|ticket)\s+(?:super\s+)?close\s+(\S+)\s+(?:"([^"]*)"|'([^']*)')/g;

function isStepId(id: string): boolean {
  return STEP_ID_RE.test(id);
}

/** True when a tool call is a tk lifecycle command (consumed as a structural
 *  marker, not rendered as work). The backend's parser decides -- the start-anchored
 *  pure-invocation rule over the UNTRUNCATED command, identical for every harness --
 *  and ships `display: "hidden"`; nothing here parses tool input. */
function isTkLifecycleCall(tc: ToolCall): boolean {
  return tc.display === "hidden";
}

/** True when an assistant message FILED a permission request -- one the user can
 *  still answer. A call the harness refused never reached the gateway, so it
 *  neither breaks out of its step nor takes a place in the queue a later verdict
 *  is matched against. */
function hasPermissionRequest(e: AssistantMessageEvent, toolResults: Map<string, ToolResultEvent>): boolean {
  return e.tool_calls.some((tc) => {
    const toolResult = toolResults.get(tc.tool_call_id) ?? null;
    return isFiledPermissionRequest(tc, toolResult) || isFiledSecretRequest(tc, toolResult);
  });
}

/** Supersede the pending secret cards this message's own secret requests replace: a
 *  newer request for the same file closes the older card in this transcript, exactly
 *  as the backend closes the older request. `pendingByFile` is the walk's running
 *  memory of the newest unresolved request per file. */
function supersedeOlderSecretRequests(
  e: AssistantMessageEvent,
  toolResults: Map<string, ToolResultEvent>,
  pendingByFile: Map<string, string>,
  resolutionsByRequestId: Map<string, RequestResolution>,
): void {
  for (const tc of e.tool_calls) {
    const details = parseSecretRequest(tc, toolResults.get(tc.tool_call_id) ?? null);
    if (details === null) continue;
    const older = pendingByFile.get(details.file);
    if (older !== undefined && older !== details.requestId && !resolutionsByRequestId.has(older)) {
      resolutionsByRequestId.set(older, "superseded");
    }
    pendingByFile.set(details.file, details.requestId);
  }
}

/** The tk lifecycle command a tool call ran, or null. The backend stamps `tk_command`
 *  (recognised off the FULL input, per harness -- claude/pi's "command", agy's
 *  "CommandLine", codex's exec program) exactly when the call carries a tk lifecycle verb,
 *  so nothing here parses tool input. */
function tkCommand(tc: ToolCall): string | null {
  return tc.tk_command ?? null;
}

interface Decoration {
  title?: string;
  summary?: string;
}

interface DecorationResult {
  deco: Map<string, Decoration>;
  /** Ids known to be steps (seen in a create / tk-step line / create input). */
  knownSteps: Set<string>;
  /** Step ids in first-created (transcript) order, for the pending roster. */
  createdOrder: string[];
}

/** Build the per-id decoration map in one global pass over all events, so a
 *  carried-over node resolves its title from a `tk start`/create line in an
 *  earlier turn. Authoritative tk output lines win; the historical input
 *  fallback only fills gaps. */
function buildDecorationMap(events: TranscriptEvent[], toolResults: Map<string, ToolResultEvent>): DecorationResult {
  const deco = new Map<string, Decoration>();
  const knownSteps = new Set<string>();
  const createdOrder: string[] = [];

  const ensure = (id: string): Decoration => {
    let d = deco.get(id);
    if (d === undefined) {
      d = {};
      deco.set(id, d);
    }
    return d;
  };
  const registerCreated = (id: string): void => {
    knownSteps.add(id);
    if (!createdOrder.includes(id)) createdOrder.push(id);
  };

  for (const e of events) {
    if (e.type !== "assistant_message") continue;
    for (const tc of e.tool_calls) {
      const command = tkCommand(tc);
      if (command === null) continue;
      // The backend stamps the tk-relevant output lines resident (`tk_stamp`); the raw
      // output never rides the event.
      const output = toolResults.get(tc.tool_call_id)?.tk_stamp ?? "";

      // Authoritative new-format output lines.
      for (const m of output.matchAll(CREATED_RE)) {
        ensure(m[1]).title = m[2].trim();
        registerCreated(m[1]);
      }
      for (const m of output.matchAll(TK_STEP_TITLE_RE)) {
        ensure(m[1]).title = m[2].trim();
        knownSteps.add(m[1]);
      }
      for (const m of output.matchAll(TK_STEP_SUMMARY_RE)) {
        ensure(m[1]).summary = m[2].trim();
        knownSteps.add(m[1]);
      }

      // Historical input fallback (fills only what the output lines did not).
      applyInputFallback(command, output, ensure, registerCreated, knownSteps);
    }
  }
  return { deco, knownSteps, createdOrder };
}

/** Recover decoration from a tk command's input (historical transcripts):
 *  - create titles: zipped positionally onto the step ids echoed in the output
 *    (the create command captured the id into a shell var, so id and title only
 *    pair by order). Skipped unless the counts match.
 *  - close summaries: id and summary are both in the command. */
function applyInputFallback(
  command: string,
  output: string,
  ensure: (id: string) => Decoration,
  registerCreated: (id: string) => void,
  knownSteps: Set<string>,
): void {
  const titles = [...command.matchAll(CREATE_TITLE_RE)].map((m) => m[1] ?? m[2]);
  if (titles.length > 0) {
    const ids = output.split(/[^A-Za-z0-9_-]+/).filter((t) => STEP_ID_RE.test(t));
    if (ids.length === titles.length) {
      for (let i = 0; i < ids.length; i++) {
        const d = ensure(ids[i]);
        if (d.title === undefined) d.title = titles[i].trim();
        registerCreated(ids[i]);
      }
    }
  }
  for (const m of command.matchAll(CLOSE_SUMMARY_RE)) {
    const id = m[1];
    if (!isStepId(id)) continue; // a regular ticket close is not a step
    const d = ensure(id);
    if (d.summary === undefined) d.summary = (m[2] ?? m[3]).trim();
    knownSteps.add(id);
  }
}

interface ParsedMessage {
  /** Start/close transitions this message caused, in order. (Creates are not
   *  positioned here -- pending steps come from the created roster.) */
  transitions: { id: string; status: "in_progress" | "closed" }[];
  /** The renderable remainder: the message stripped of its tk lifecycle calls.
   *  Null when nothing renderable remains (a pure tk command). */
  render: AssistantMessageEvent | null;
}

/** Split an assistant message into the tk transitions it caused and the
 *  renderable remainder (text + non-tk tool calls). */
function parseMessage(e: AssistantMessageEvent, toolResults: Map<string, ToolResultEvent>): ParsedMessage {
  // Only actual lifecycle commands can change the timeline. A Read/search result
  // can quote another agent's commands or even a whole transcript verbatim.
  // Non-pure invocations (e.g. `cd x && tk close s1`) still carry tk_command.
  const transitions: { id: string; status: "in_progress" | "closed" }[] = [];
  for (const tc of e.tool_calls) {
    if (tkCommand(tc) === null) continue;
    const output = toolResults.get(tc.tool_call_id)?.tk_stamp ?? "";
    TK_UPDATED_RE.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = TK_UPDATED_RE.exec(output)) !== null) {
      const status = match[2];
      if (status === "in_progress" || status === "closed") {
        transitions.push({ id: match[1], status });
      }
    }
  }

  // Only a recognised, pure tk lifecycle call is hidden from the rendered
  // output. Anything else (including a command that merely mentions a tk verb)
  // renders as normal work, so real work is never silently dropped.
  const realCalls = e.tool_calls.filter((tc) => !isTkLifecycleCall(tc));
  if (realCalls.length === e.tool_calls.length) {
    return { transitions, render: e };
  }
  // Pure tk command (no text, no real work): fully consumed.
  if (!e.text && realCalls.length === 0) {
    return { transitions, render: null };
  }
  return { transitions, render: { ...e, tool_calls: realCalls } };
}

/** True when a renderable message represents real work (issues a non-tk tool
 *  call), as opposed to prose. */
function isWork(e: AssistantMessageEvent): boolean {
  return e.tool_calls.length > 0;
}

/** True when a renderable message is prose (has text, no tool calls). */
function isProse(e: AssistantMessageEvent): boolean {
  return !!e.text && e.tool_calls.length === 0;
}

// Section assembly

/** An ordered skeleton entry of a conversation section, recorded as the transcript is walked.
 *  An `event` entry is a routed assistant message; one routed to a step renders inside that
 *  step's node unless it is the step's closing prose. Items are rebuilt from these in
 *  transcript order. */
type SectionEntry =
  /** A permission request, lifted out of any open step to render inline as a
   *  visible break (see hasPermissionRequest / the `permission` TimelineItem). */
  | { kind: "permission"; event: AssistantMessageEvent }
  /** A system chip or notice that landed inside an open handoff node. */
  | { kind: "chip"; event: UserMessageEvent }
  /** A compaction, landed or running. */
  | { kind: "status"; part: CompactionPart }
  /** The handoff node, at the summary request that opened it or at a switch with no request. */
  | { kind: "handoff"; node: HandoffNode }
  | { kind: "event"; event: AssistantMessageEvent; step_id: string | null };

/** Returns true if an entry represents conversation progress or work rather than a trailing status. */
function isSubsequentTurnActivity(entry: SectionEntry): boolean {
  return entry.kind !== "status";
}

interface SectionBuilder {
  kind: "section";
  user_event: UserMessageEvent | null;
  key: string;
  /** Ordered skeleton of chips and routed events (see SectionEntry). */
  entries: SectionEntry[];
  /** The handoff node open in this section: every assistant message from the summary request
   *  on belongs to it, until the switch closes it. */
  open_handoff: HandoffNode | null;
}

/** One timeline: every step from the first that appears while none is open until each of
 *  them is done. */
interface TimelineBuilder {
  kind: "timeline";
  key: string;
  steps: Map<string, StepNode>;
  /** Steps that have started or closed, in the order they first did. */
  step_order: string[];
  /** Steps created on this timeline that have not started or closed yet, in creation order. */
  pending: string[];
  current_step_id: string | null;
}

function newSection(user_event: UserMessageEvent | null, key: string): SectionBuilder {
  return { kind: "section", user_event, key, entries: [], open_handoff: null };
}

function newTimeline(key: string): TimelineBuilder {
  return { kind: "timeline", key, steps: new Map(), step_order: [], pending: [], current_step_id: null };
}

/** Whether every step on the timeline is done: it freezes in place. */
function isTimelineDone(timeline: TimelineBuilder): boolean {
  return timeline.pending.length === 0 && timeline.step_order.every((id) => timeline.steps.get(id)!.status === "done");
}

/** The step ids a message's tk calls created, in order. */
function createdStepIds(e: AssistantMessageEvent, toolResults: Map<string, ToolResultEvent>): string[] {
  const ids: string[] = [];
  for (const tc of e.tool_calls) {
    if (tkCommand(tc) === null) continue;
    const output = toolResults.get(tc.tool_call_id)?.tk_stamp ?? "";
    for (const m of output.matchAll(CREATED_RE)) ids.push(m[1]);
  }
  return ids;
}

/** True for a transcript event that carries the user's own words: a ``user_message`` with no display
 *  decision, one sent with machine context stripped for display (a seeded chat's first send, or a
 *  turn a harness flushed together with background-task reports), or the
 *  handoff prompt a successor started with (a chip that holds the message the user switched with).
 *  The backend's rule for whether a handoff has anything to summarize (``has_user_turn``), read
 *  here so the page can tell a fresh chat, whose switch needs no dialog, from one with context. */
export function isGenuineUserTurn(event: TranscriptEvent): boolean {
  if (event.type !== "user_message") return false;
  return event.display === undefined || isPromptWithContext(event) || isHandoffPromptChip(event);
}

export function hasUserTurn(events: readonly TranscriptEvent[]): boolean {
  return events.some(isGenuineUserTurn);
}

/** The successor's opening turn, built from the switch that carries the message the user switched
 *  with: folded into the successor's prompt, that message is no event of the transcript's own. Null
 *  for a switch that carries none (a fresh start's message arrives as a turn of its own). */
export function openingTurnOf(event: AgentSwitchEvent): UserMessageEvent | null {
  if (event.message === null || event.message_id === null) return null;
  return {
    type: "user_message",
    event_id: `${event.event_id}:message`,
    timestamp: event.timestamp,
    source: event.source,
    role: "user",
    content: event.message,
  };
}

/** Whether a summary request on the transcript is the chat's live switch's own, rather than one an
 *  earlier switch sent before it was called off: the live switch asked after it was confirmed. Timestamps
 *  that do not parse cannot be told apart, and read as the live switch's. */
export function isLiveHandoffRequest(request: UserMessageEvent, handoff: HandoffState | null): boolean {
  if (handoff === null) return false;
  const requestedAt = Date.parse(request.timestamp);
  const startedAt = Date.parse(handoff.started_at);
  if (Number.isNaN(requestedAt) || Number.isNaN(startedAt)) return true;
  return requestedAt >= startedAt;
}

/** Whether the transcript's live segment holds the live switch's summary request, with no switch
 *  closing it yet: the handoff node the walk builds from it is then the one showing the switch's
 *  progress, and the page needs no node of its own for it. */
export function hasOpenHandoffRequest(events: readonly TranscriptEvent[], handoff: HandoffState | null): boolean {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.type === "agent_switch") return false;
    if (event.type === "user_message" && isHandoffSummaryRequest(event)) return isLiveHandoffRequest(event, handoff);
  }
  return false;
}

/** Whether the tail turn renders as settled (no frontier spinner, closing prose as the reply) for
 *  an agent in this activity state. A compaction counts as settled: an idle compaction runs with no
 *  turn open, and one inside a running turn only hides that turn's frontier until it resumes. */
export function isTailTurnSettled(activityState: string | null | undefined): boolean {
  return activityState === "IDLE" || activityState === COMPACTING_STATE;
}

/** A compaction the agent is running now, by who started it. */
export interface LiveCompaction {
  cause: CompactionCause | null;
}

/** The compaction an agent in this activity state is running, or null when it is not compacting. */
export function liveCompactionOf(
  activityState: string | null | undefined,
  cause: CompactionCause | null | undefined,
): LiveCompaction | null {
  return activityState === COMPACTING_STATE ? { cause: cause ?? null } : null;
}

/** Whether the transcript ends on a compaction that landed: nothing after it but lines that
 *  render nowhere. The running compaction's event is then on the transcript already, so the
 *  walk places no running one beside it. */
function endsOnLandedCompaction(events: readonly TranscriptEvent[]): boolean {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.type !== "user_message") return false;
    if (isStatusUserMessage(event)) return true;
    if (!isNonBoundaryUserMessage(event)) return false;
  }
  return false;
}

/** The latest idle compaction's event id, which the one-time Auto-compact notice sits under. */
function autocompactNoticeAnchorId(events: readonly TranscriptEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.type === "user_message" && isStatusUserMessage(event) && event.compaction_cause === "idle") {
      return event.event_id;
    }
  }
  return null;
}

/** Walk the visible transcript into ordered sections. `toolResults` resolves tk
 *  command outputs (and is reused by the renderer). `isTailSettled` (see
 *  `isTailTurnSettled`) settles the spinner on the live timeline. `liveCompaction` (see
 *  `liveCompactionOf`) places the compaction running now where its event will land.
 *  All decoration is derived from the transcript; there is no enrichment argument. */
export function buildSections(
  events: TranscriptEvent[],
  toolResults: Map<string, ToolResultEvent>,
  isTailSettled: boolean,
  liveCompaction: LiveCompaction | null = null,
): SectionView[] {
  const { deco, knownSteps, createdOrder } = buildDecorationMap(events, toolResults);
  const noticeAnchorId = autocompactNoticeAnchorId(events);

  // Conversation sections and frozen timelines, in the order they render.
  const blocks: (SectionBuilder | TimelineBuilder)[] = [];
  let current: SectionBuilder | null = null;
  // The timeline still holding a step that is not done. It renders after every block.
  let live: TimelineBuilder | null = null;
  // Every step id some timeline holds, so the pending roster below adds only the rest.
  const placedStepIds = new Set<string>();
  // The primary correlation path: every id-keyed verdict, keyed by the resolved
  // request's own id (PermissionRequestDetails.requestId) -- shared across the
  // whole transcript and looked up per-card at render time, so it is immune to
  // requests being resolved out of the order they were created, and to more
  // than one permission request being batched into one message.
  const resolutionsByRequestId = new Map<string, RequestResolution>();
  // The newest secret request per file that has not resolved yet, so a later request
  // for the same file can mark the earlier card superseded.
  const pendingSecretRequestByFile = new Map<string, string>();
  const secretNotesByRequestId = new Map<string, string>();
  const ensureSection = (user_event: UserMessageEvent | null, key: string): SectionBuilder => {
    const section = newSection(user_event, key);
    blocks.push(section);
    return section;
  };

  // The node a switch just closed, until the successor's first turn-making event: the handoff
  // prompt lands on it rather than in the successor's section. Hidden lines the successor's
  // setup sends before the prompt (a model pick's slash commands) leave it armed.
  let lastSwitched: HandoffNode | null = null;
  for (const e of events) {
    if (e.type === "agent_switch" && e.from_harness === SEED_HARNESS) {
      // The chat's first agent taking over from the seed segment, which no agent wrote: nothing
      // was handed off, so no node marks it; the agent's first turn simply opens a section.
      current = ensureSection(openingTurnOf(e), `section-switch-${e.event_id}`);
      continue;
    }
    if (e.type === "agent_switch") {
      // The chat moved to another agent. The switch closes the handoff node the summary
      // request opened (the node stays where the request was, at the end of the retiring
      // agent's last turn); with no request (a fresh start) the switch is the node. The
      // successor's section opens on the message the user switched with when the switch
      // carries it, else with no bubble.
      if (current === null) current = ensureSection(null, "section-pre");
      if (current.open_handoff !== null) {
        current.open_handoff.switch = e;
        lastSwitched = current.open_handoff;
        current.open_handoff = null;
      } else {
        lastSwitched = { key: e.event_id, request: null, events: [], switch: e, prompt: null };
        current.entries.push({ kind: "handoff", node: lastSwitched });
      }
      current = ensureSection(openingTurnOf(e), `section-switch-${e.event_id}`);
      continue;
    }
    if (e.type === "user_message") {
      if (lastSwitched !== null && isHandoffPromptChip(e)) {
        // The prompt the switch started the successor with: it belongs to the handoff, so the
        // node shows it and the successor's section opens on its reply.
        lastSwitched.prompt = e;
        lastSwitched = null;
        continue;
      }
      if (isHandoffSummaryRequest(e)) {
        lastSwitched = null;
        // The chat app asked the agent for its handoff summary: the node opens here and takes
        // the agent's answer, so the pause reads as one thing rather than a chip and a write.
        if (current === null) current = ensureSection(null, "section-pre");
        const node: HandoffNode = { key: e.event_id, request: e, events: [], switch: null, prompt: null };
        current.entries.push({ kind: "handoff", node });
        current.open_handoff = node;
        continue;
      }
      // A granted/denied notification for an earlier permission request. Record
      // the verdict (it reflects on that request's card, not as a user prompt) and
      // open a fresh section with no user bubble: the raw text is not shown.
      //
      // The notification's own request id is the correlation key. A
      // notification with NO id (recorded before Imbue Studio embedded ids)
      // attributes nothing -- guessing by arrival order is what swapped
      // verdicts between cards, and an embedded page recovers the verdict
      // from the response log instead (the card's hydration query).
      const resolution = resolutionOf(e);
      if (resolution !== null) {
        lastSwitched = null;
        const requestId = resolutionRequestIdOf(e);
        if (requestId !== null) resolutionsByRequestId.set(requestId, resolution);
        current = ensureSection(null, `section-after-${e.event_id}`);
        continue;
      }
      // The chat app's notice that a secret card was answered: the same break as a
      // permission verdict, keyed by the request's own id (a secret notice always
      // carries one).
      const secretResolution = secretResolutionOf(e);
      if (secretResolution !== null) {
        lastSwitched = null;
        const requestId = secretResolutionRequestIdOf(e);
        if (requestId !== null) resolutionsByRequestId.set(requestId, secretResolution);
        const note = secretResolutionNoteOf(e);
        if (requestId !== null && note !== null) secretNotesByRequestId.set(requestId, note);
        current = ensureSection(null, `section-after-${e.event_id}`);
        continue;
      }
      if (
        current !== null &&
        current.open_handoff !== null &&
        (isSystemChipUserMessage(e) || isNoticeUserMessage(e))
      ) {
        // The handoff node takes the retiring agent's whole summary turn, so a chip landing
        // inside it stays in that turn, at its spot after the node.
        current.entries.push({ kind: "chip", event: e });
        continue;
      }
      if (isStatusUserMessage(e)) {
        if (current === null) current = ensureSection(null, "section-pre");
        current.entries.push({ kind: "status", part: landedCompaction(e, e.event_id === noticeAnchorId) });
        continue;
      }
      if (isNonBoundaryUserMessage(e)) {
        // Skill expansions and hidden framework injections (/welcome, image notes, resume
        // markers) render nowhere on the user rail, so they are dropped here.
        continue;
      }

      // A boundary -- a real user turn, or a system chip or notice (see the
      // module docstring): open a new section headed by the message.
      lastSwitched = null;
      current = ensureSection(e, `section-${e.event_id}`);
      continue;
    }
    if (e.type === "assistant_message") {
      lastSwitched = null;
      if (current === null) current = ensureSection(null, "section-pre");
      if (current.open_handoff !== null) {
        // The retiring agent's answer to the summary request, whole: the node shows it.
        current.open_handoff.events.push(e);
        continue;
      }
      const parsed = parseMessage(e, toolResults);
      // Only step records render in the timeline. A `tk start`/`tk close` on a
      // regular ticket the agent picked up prints the same
      // `Updated <id> -> <status>` line as a step does; skip it (it is neither
      // a `-step-` id nor a known step) rather than mint a phantom node titled
      // with the raw ticket id. Any work batched with that command stays in
      // the step that was already open (or renders inline if none was).
      const transitions = parsed.transitions.filter((t) => isStepId(t.id) || knownSteps.has(t.id));
      const created = createdStepIds(e, toolResults);
      if (live === null && (created.length > 0 || transitions.length > 0)) {
        live = newTimeline(`timeline-${e.event_id}`);
      }
      // Route this message's content to the step it belongs to: the step it
      // opened (work shares a message with its `tk start`), or else the step
      // that was current before the message (work shares a message with a
      // `tk close` -- it stays in the closing step).
      const stepBefore = live?.current_step_id ?? null;
      let lastOpened: string | null = null;
      if (live !== null) {
        for (const id of created) {
          if (!live.steps.has(id) && !live.pending.includes(id)) live.pending.push(id);
          placedStepIds.add(id);
        }
        // Apply transitions in transcript order so each step node lands at its
        // real position -- a batched `tk close a && tk start b` must keep a's node
        // before b's.
        for (const t of transitions) {
          applyTransition(live, t);
          placedStepIds.add(t.id);
          if (t.status === "in_progress") lastOpened = t.id;
        }
      }
      if (parsed.render !== null && (parsed.render.text || parsed.render.tool_calls.length > 0)) {
        if (hasPermissionRequest(parsed.render, toolResults)) {
          // A permission or secret request breaks out of any open step: it must
          // always be directly visible, never collapsed inside a step node. The step
          // stays open (current_step_id is untouched), so work resumed after the user
          // responds keeps grouping under it.
          supersedeOlderSecretRequests(parsed.render, toolResults, pendingSecretRequestByFile, resolutionsByRequestId);
          current.entries.push({ kind: "permission", event: parsed.render });
        } else {
          routeMessage(current, live, parsed.render, lastOpened ?? stepBefore);
        }
      }
      if (live !== null && isTimelineDone(live)) {
        // Every step is done: the timeline freezes here, and what follows renders below it.
        blocks.push(live);
        current = ensureSection(null, `section-after-${live.key}`);
        live = null;
      }
      continue;
    }
    // tool_result events are resolved by id via toolResults; no routing needed.
  }

  // The compaction running now goes where its event will land: at the end of the transcript.
  if (liveCompaction !== null && !endsOnLandedCompaction(events)) {
    if (current === null) current = ensureSection(null, "section-pre");
    current.entries.push({ kind: "status", part: runningCompaction(liveCompaction.cause) });
  }

  // Created steps no walk placed (a historical create whose id only the input fallback paired
  // with its title) wait on the live timeline, in creation order.
  const unplaced = createdOrder.filter((id) => !placedStepIds.has(id));
  if (unplaced.length > 0) {
    if (live === null) live = newTimeline("timeline-tail");
    live.pending.push(...unplaced);
  }
  if (live !== null) blocks.push(live);

  const frontierId = live !== null && !isTailSettled ? live.current_step_id : null;
  const ejectedIds = new Set<string>();
  for (const block of blocks) {
    if (block.kind === "timeline") collectEjectedProse(block, block === live ? frontierId : null, ejectedIds);
  }
  return blocks.map((block) =>
    block.kind === "timeline"
      ? finalizeTimeline(block, deco, ejectedIds, block === live ? frontierId : null)
      : finalizeSection(block, ejectedIds, resolutionsByRequestId, secretNotesByRequestId),
  );
}

/** Open (or re-open) a step node as the current step. */
function openStep(timeline: TimelineBuilder, id: string): void {
  const existing = timeline.steps.get(id);
  if (existing === undefined) {
    timeline.steps.set(id, {
      ticket_id: id,
      title: id,
      status: "active",
      summary: null,
      narration: null,
      is_frontier: false,
      events: [],
    });
    timeline.step_order.push(id);
  } else if (existing.status === "done") {
    // Re-opened (a `tk start` on a previously-closed id on this timeline): it is
    // active again, so it must not keep showing as done/settled.
    existing.status = "active";
    existing.summary = null;
  }
  timeline.current_step_id = id;
}

function applyTransition(timeline: TimelineBuilder, t: { id: string; status: "in_progress" | "closed" }): void {
  timeline.pending = timeline.pending.filter((id) => id !== t.id);
  if (t.status === "in_progress") {
    openStep(timeline, t.id);
    return;
  }
  // closed
  const node = timeline.steps.get(t.id);
  if (node !== undefined) {
    node.status = "done";
  } else {
    // A close with no preceding start on this timeline (e.g. created and closed
    // before any start was observed): still surface it as a done node.
    timeline.steps.set(t.id, {
      ticket_id: t.id,
      title: t.id,
      status: "done",
      summary: null,
      narration: null,
      is_frontier: false,
      events: [],
    });
    timeline.step_order.push(t.id);
  }
  if (timeline.current_step_id === t.id) timeline.current_step_id = null;
}

function routeMessage(
  section: SectionBuilder,
  timeline: TimelineBuilder | null,
  e: AssistantMessageEvent,
  step_id: string | null,
): void {
  section.entries.push({ kind: "event", event: e, step_id });
  if (step_id !== null) {
    timeline?.steps.get(step_id)?.events.push(e);
  }
}

/** Collect each step's closing remarks into `ejected`: the prose it spoke after its last
 *  work. The live frontier step is exempt: nothing has ended it, so its trailing prose is
 *  in-flight narration shown as a caption. */
function collectEjectedProse(timeline: TimelineBuilder, frontierId: string | null, ejected: Set<string>): void {
  for (const id of timeline.step_order) {
    if (id === frontierId) continue;
    const events = timeline.steps.get(id)!.events;
    let lastWorkIdx = -1;
    for (let i = 0; i < events.length; i++) if (isWork(events[i])) lastWorkIdx = i;
    for (let i = lastWorkIdx + 1; i < events.length; i++) {
      if (isProse(events[i])) ejected.add(events[i].event_id);
    }
  }
}

/** Finalize a timeline: drop each step's ejected closing prose, attribute narration, join
 *  decoration, and emit its steps, then the steps still waiting to start. */
function finalizeTimeline(
  timeline: TimelineBuilder,
  deco: Map<string, Decoration>,
  ejectedIds: ReadonlySet<string>,
  frontierId: string | null,
): SectionView {
  const items: TimelineItem[] = [];
  for (const id of timeline.step_order) {
    const node = timeline.steps.get(id)!;
    node.events = node.events.filter((ev) => !ejectedIds.has(ev.event_id));
    // Narration: the latest in-step prose -- the live caption under the step. For a
    // non-frontier step this is the latest prose followed by more work in the same step
    // (its trailing closing prose was ejected); for the live frontier step it is simply the
    // last thing the agent said, so a just-spoken line shows even before the next tool call.
    let narration: string | null = null;
    for (const ev of node.events) {
      if (isProse(ev)) narration = ev.text;
    }
    node.narration = narration;
    // A node with no decoration entry keeps the raw id as its title and has no summary (its
    // create/tk-step lines scrolled out of the loaded window -- an accepted loss for very
    // old steps).
    const d = deco.get(id);
    if (d?.title) node.title = d.title;
    if (node.status === "done") node.summary = d?.summary ?? null;
    node.is_frontier = node.ticket_id === frontierId && node.status === "active";
    items.push({ kind: "step", step: node });
  }
  for (const id of timeline.pending) {
    items.push({
      kind: "step",
      step: {
        ticket_id: id,
        title: deco.get(id)?.title ?? id,
        status: "pending",
        summary: null,
        narration: null,
        is_frontier: false,
        events: [],
      },
    });
  }
  return { user_event: null, key: timeline.key, items, trailing_reply: [], trailing_status: [] };
}

/** Finalize a conversation section: pull out the trailing reply and emit its items in
 *  transcript order. A step's work renders inside the step's node, so only the closing prose
 *  ejected from a step joins the conversation, at its transcript position. */
function finalizeSection(
  section: SectionBuilder,
  ejectedIds: ReadonlySet<string>,
  resolutionsByRequestId: ReadonlyMap<string, RequestResolution>,
  secretNotesByRequestId: ReadonlyMap<string, string>,
): SectionView {
  // A fresh start asked for no summary and delivered no prompt: there was no handoff to show, so
  // its node comes off the timeline rather than standing as an empty line.
  const entries = section.entries.filter(
    (entry) =>
      !(entry.kind === "handoff" && isFreshStartNode(entry.node)) &&
      !(entry.kind === "event" && entry.step_id !== null && !ejectedIds.has(entry.event.event_id)),
  );

  // Trailing reply: the final run of prose after the reply boundary (the last real work, a
  // permission request, a handoff node, or a compaction with activity after it).
  let replyBoundary = -1;
  for (let i = 0; i < entries.length; i++) {
    const en = entries[i];
    if (en.kind === "permission" || en.kind === "handoff") replyBoundary = i;
    else if (en.kind === "event" && isWork(en.event)) replyBoundary = i;
    else if (en.kind === "status" && entries.slice(i + 1).some(isSubsequentTurnActivity)) replyBoundary = i;
  }
  const trailingIds = new Set<string>();
  const trailing_reply: AssistantMessageEvent[] = [];
  let lastTrailingReplyEntryIdx = replyBoundary;
  for (let i = replyBoundary + 1; i < entries.length; i++) {
    const en = entries[i];
    if (en.kind !== "event" || !isProse(en.event)) continue;
    trailing_reply.push(en.event);
    trailingIds.add(en.event.event_id);
    lastTrailingReplyEntryIdx = i;
  }

  // A compaction after the reply follows it. With no reply to follow, it stays in the inline
  // stream, where its chips join the turn's last chip row.
  const trailing_status: CompactionPart[] = [];
  const trailingStatusIndices = new Set<number>();
  for (let i = lastTrailingReplyEntryIdx + 1; i < entries.length && trailing_reply.length > 0; i++) {
    const en = entries[i];
    if (en.kind === "status") {
      trailing_status.push(en.part);
      trailingStatusIndices.add(i);
    }
  }

  // Ungrouped events coalesce into inline runs; permission cards, chips and handoff nodes each
  // stand at their own transcript position.
  const items: TimelineItem[] = [];
  let ungrouped: RunPart[] = [];
  let ungroupedKey = 0;
  const flushUngrouped = (): void => {
    if (ungrouped.length > 0) {
      items.push({ kind: "ungrouped", key: `${section.key}-ung-${ungroupedKey++}`, events: ungrouped });
      ungrouped = [];
    }
  };
  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    if (entry.kind === "permission") {
      // Each card looks up its own verdict in resolutionsByRequestId by its own request id.
      flushUngrouped();
      items.push({ kind: "permission", event: entry.event, resolutionsByRequestId, secretNotesByRequestId });
    } else if (entry.kind === "chip") {
      flushUngrouped();
      items.push({ kind: "chip", event: entry.event });
    } else if (entry.kind === "status") {
      // A compaction joins the inline run, so its chips share a row with the calls beside it.
      if (!trailingStatusIndices.has(i)) ungrouped.push(entry.part);
    } else if (entry.kind === "handoff") {
      flushUngrouped();
      items.push({ kind: "handoff", node: entry.node });
    } else if (!trailingIds.has(entry.event.event_id)) {
      ungrouped.push(entry.event);
    }
  }
  flushUngrouped();

  return { user_event: section.user_event, key: section.key, items, trailing_reply, trailing_status };
}
