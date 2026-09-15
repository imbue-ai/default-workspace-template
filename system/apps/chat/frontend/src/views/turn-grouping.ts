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
 *   pass over all events into a per-id decoration map, so a carried-over node
 *   resolves its title from the `tk start`/create line in an earlier turn.
 * - For *historical* transcripts that predate those output lines, a best-effort
 *   input-preview fallback recovers titles/summaries from the tk command inputs
 *   (`tk create --step "Title"`, `tk close <id> "summary"`). It is allowed to be
 *   imperfect; it serves a frozen corpus only.
 *
 * The walk maintains a single "current open step": events while a step is open
 * group under it; events while none is open fall into an ungrouped run rendered
 * inline (the same plain-chat path used for turns with no steps). When a step
 * closes, any prose it spoke after its last work is ejected into the ungrouped
 * inline stream right after the step node, so a closing remark is promoted out
 * of the step rather than buried in it. The turn's wrap-up reply is simply the
 * final run of ungrouped prose, rendered below the timeline.
 *
 * A system chip (Stop-hook feedback, a background-task notice, a fleet nudge)
 * splits a step into *stints*, and that ejection runs per stint. Prose closing a
 * stint is a delivered reply, and the chip's POSITION is what proves it: a chip
 * is a user-side line, so it only arrives at a request boundary, which leaves it
 * two possible spots. Either it follows a tool result -- the stint ends in work
 * and ejection has nothing to take -- or it follows prose, which can only happen
 * when that response ended with text and no tool call, i.e. the agent stopped
 * there. Work it does once woken must not retroactively bury that reply inside
 * the step that happened to still be open.
 *
 * A step still open when the next user message arrives carries over: it
 * re-renders at the top of the new turn, while the prior turn's node freezes at
 * its last-known state.
 *
 * One message is never grouped under a step: an agent permission request. It
 * is lifted out into a dedicated inline break (the `permission` timeline item)
 * so the user always sees it and can respond without expanding a step. The step
 * it interrupted stays open, so work resumed afterwards keeps grouping under it.
 * When the user later grants or denies the request, the app injects a plain
 * notification user message. The walk reads its verdict onto the request's card
 * and treats the notification as a turn boundary (the agent blocked on the
 * request and is now resuming), so any open step carries over and continues in
 * the new turn rather than the same node resuming beneath the card. The raw text
 * isn't shown (its verdict is on the card). The notification carries the
 * resolved request's own id (see message-classification's resolutionRequestIdOf),
 * so a card looks up its verdict by that id rather than by arrival order: two
 * requests resolved out of the order they were created never swap their
 * cards' verdicts, and a message that batches more than one permission request
 * resolves each of its cards independently. A notification with no id (recorded
 * before minds embedded ids) attributes nothing here -- an embedded page
 * recovers such verdicts from the response log via the card's hydration query.
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
} from "../models/Response";
import type { PermissionResolution } from "./message-classification";
import { isFiledPermissionRequest } from "./permission-card";
import {
  isHandoffPromptChip,
  isHandoffSummaryRequest,
  isNonBoundaryUserMessage,
  isSystemChipUserMessage,
  resolutionOf,
  resolutionRequestIdOf,
} from "./message-classification";

export type StepStatus = "pending" | "active" | "done";

/** A step as it should render in one section. The same ticket id can produce
 *  two independent nodes across two sections (carryover); each holds its own
 *  state and never updates the other. */
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
  /** True when this node carried over from a prior section (re-rendered at the
   *  top of this section because it was still open at the boundary). */
  is_carryover: boolean;
  /** True when this is the live step the agent is currently on -- the only one
   *  that may show a spinner. False once settled (idle, past section, or
   *  superseded by a later step). */
  is_frontier: boolean;
  /** The grouped real-work events (assistant text + non-tk tool calls) that
   *  occurred while this step was the open step in this section. */
  events: AssistantMessageEvent[];
}

/** The handoff between two agents of one chat, as one node: the chat app's summary request, the
 *  retiring agent's work in answer (the summary write), and the switch that closed it. Open while
 *  the switch has not landed: a handoff still running, one that failed, or one called off. */
export interface HandoffNode {
  key: string;
  /** The summary request that opened the node; null for a node the switch alone made (a fresh
   *  start asked for no summary). */
  request: UserMessageEvent | null;
  /** The retiring agent's turn after the request: what expanding the node shows. */
  events: AssistantMessageEvent[];
  /** The switch that completed the handoff, once it has. */
  switch: AgentSwitchEvent | null;
  /** The prompt the successor was started with (the chip carrying the summary and the user's message),
   *  once it has reached the successor's segment: shown inside the node rather than as a chip of its own. */
  prompt: UserMessageEvent | null;
}

/** One item on a section's timeline, in transcript order. */
export type TimelineItem =
  | { kind: "step"; step: StepNode }
  /** Real work and/or prose with no step open: pre-step work, or a step's
   *  ejected closing prose. Rendered inline, exactly like a no-steps turn. */
  | { kind: "ungrouped"; key: string; events: AssistantMessageEvent[] }
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
      resolutionsByRequestId: ReadonlyMap<string, PermissionResolution>;
    }
  /** A non-boundary user message shown inline (e.g. a stop-hook chip). */
  | { kind: "chip"; event: UserMessageEvent }
  /** The chat's handoff to another agent, at the point its summary was asked for (or, with no
   *  summary, at the switch itself). */
  | { kind: "handoff"; node: HandoffNode };

/** A turn: the user message, its timeline, and the wrap-up reply below it. */
export interface SectionView {
  /** The boundary user message that opened this section, or null for content
   *  that precedes the first user message. */
  user_event: UserMessageEvent | null;
  key: string;
  items: TimelineItem[];
  /** The final run of ungrouped prose: the user-facing reply, rendered below
   *  the timeline. */
  trailing_reply: AssistantMessageEvent[];
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
  return e.tool_calls.some((tc) => isFiledPermissionRequest(tc, toolResults.get(tc.tool_call_id) ?? null));
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
  // output. Anything else -- including a command that merely mentions a tk verb
  // -- renders as normal work, so real work is never silently dropped.
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

// --- Section assembly ---

/** An ordered skeleton entry recorded as the transcript is walked. A `step`
 *  entry marks where a step node first appears -- its first transition (an
 *  open, or a close with no prior open) -- so the node is positioned by that
 *  transition even when the step carries no work. An `event` entry is a routed
 *  assistant message. Timeline items are rebuilt from these in transcript
 *  order. */
type SectionEntry =
  | { kind: "step"; id: string }
  /** A permission request, lifted out of any open step to render inline as a
   *  visible break (see hasPermissionRequest / the `permission` TimelineItem). */
  | { kind: "permission"; event: AssistantMessageEvent }
  /** A collapsed system chip. It sits in the skeleton because its POSITION is
   *  what lets the ejection pass tell a delivered reply from mid-step narration
   *  (see collectEjectedProse). */
  | { kind: "chip"; event: UserMessageEvent }
  /** The handoff node, at the summary request that opened it or at a switch with no request. */
  | { kind: "handoff"; node: HandoffNode }
  | { kind: "event"; event: AssistantMessageEvent; step_id: string | null };

interface SectionBuilder {
  user_event: UserMessageEvent | null;
  key: string;
  /** Step nodes in first-appearance (transcript) order. */
  steps: Map<string, StepNode>;
  step_order: string[];
  /** Ordered skeleton of step appearances, chips and routed events (see
   *  SectionEntry), used to assemble timeline items in transcript order. */
  entries: SectionEntry[];
  current_step_id: string | null;
  /** The handoff node open in this section: every assistant message from the summary request
   *  on belongs to it, until the switch closes it. */
  open_handoff: HandoffNode | null;
}

function newSection(user_event: UserMessageEvent | null, key: string): SectionBuilder {
  return {
    user_event,
    key,
    steps: new Map(),
    step_order: [],
    entries: [],
    current_step_id: null,
    open_handoff: null,
  };
}

/** True for a transcript event that carries the user's own words: a ``user_message`` with no display
 *  decision, or the handoff prompt a successor started with (a chip that holds the message the user
 *  switched with). The backend's rule for whether a handoff has anything to summarize
 *  (``has_user_turn``), read here so the page can tell a fresh chat, whose switch needs no dialog,
 *  from one with context. */
export function isGenuineUserTurn(event: TranscriptEvent): boolean {
  if (event.type !== "user_message") return false;
  return event.display === undefined || isHandoffPromptChip(event);
}

export function hasUserTurn(events: readonly TranscriptEvent[]): boolean {
  return events.some(isGenuineUserTurn);
}

/** Whether the transcript's live segment holds a summary request no switch has closed: the handoff
 *  node the walk builds from it is then the one showing the switch's progress, and the page needs
 *  no node of its own for it. */
export function hasOpenHandoffRequest(events: readonly TranscriptEvent[]): boolean {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.type === "agent_switch") return false;
    if (event.type === "user_message" && isHandoffSummaryRequest(event)) return true;
  }
  return false;
}

/** Walk the visible transcript into ordered sections. `toolResults` resolves tk
 *  command outputs (and is reused by the renderer). `agentIsIdle` settles the
 *  spinner on the tail section. All decoration is derived from the transcript;
 *  there is no enrichment argument. */
export function buildSections(
  events: TranscriptEvent[],
  toolResults: Map<string, ToolResultEvent>,
  agentIsIdle: boolean,
): SectionView[] {
  const { deco, knownSteps, createdOrder } = buildDecorationMap(events, toolResults);

  const builders: SectionBuilder[] = [];
  let current: SectionBuilder | null = null;
  // Steps open at the end of the prior section, to re-open as carryover.
  let carryover: string[] = [];
  // The primary correlation path: every id-keyed verdict, keyed by the resolved
  // request's own id (PermissionRequestDetails.requestId) -- shared across the
  // whole transcript and looked up per-card at render time, so it is immune to
  // requests being resolved out of the order they were created, and to more
  // than one permission request being batched into one message.
  const resolutionsByRequestId = new Map<string, PermissionResolution>();
  const ensureSection = (user_event: UserMessageEvent | null, key: string): SectionBuilder => {
    const section = newSection(user_event, key);
    // Re-open carried-over steps at the top of the new section.
    for (const id of carryover) {
      openStep(section, id, /* is_carryover */ true);
    }
    carryover = [];
    builders.push(section);
    return section;
  };

  // The node a switch just closed, until the successor's first turn-making event: the handoff
  // prompt lands on it rather than in the successor's section. Hidden lines the successor's
  // setup sends before the prompt (a model pick's slash commands) leave it armed.
  let lastSwitched: HandoffNode | null = null;
  for (const e of events) {
    if (e.type === "agent_switch") {
      // The chat moved to another agent. The switch closes the handoff node the summary
      // request opened (the node stays where the request was, at the end of the retiring
      // agent's last turn); with no request (a fresh start) the switch is the node. The new
      // agent starts fresh, so the switch is a turn boundary: the prior section closes,
      // carrying whatever was still open, and a bubble-less section opens.
      if (current === null) current = ensureSection(null, "section-pre");
      if (current.open_handoff !== null) {
        current.open_handoff.switch = e;
        lastSwitched = current.open_handoff;
        current.open_handoff = null;
      } else {
        lastSwitched = { key: e.event_id, request: null, events: [], switch: e, prompt: null };
        current.entries.push({ kind: "handoff", node: lastSwitched });
      }
      carryover = openStepsAtEnd(current);
      current = ensureSection(null, `section-switch-${e.event_id}`);
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
      // the verdict (it reflects on that request's card, not as a user prompt),
      // then treat the notification as the turn boundary it naturally is: the
      // agent blocked on the request and is now resuming, so close the current
      // section -- carrying any open step over -- and open a fresh one. The step
      // then continues in the normal carryover way rather than the same node
      // resuming inline beneath the card. The raw text is not shown (its verdict
      // is on the card), so the new section has no user bubble.
      //
      // The notification's own request id is the correlation key. A
      // notification with NO id (recorded before minds embedded ids)
      // attributes nothing -- guessing by arrival order is what swapped
      // verdicts between cards, and an embedded page recovers the verdict
      // from the response log instead (the card's hydration query). The
      // break-with-no-bubble applies either way.
      const resolution = resolutionOf(e);
      if (resolution !== null) {
        lastSwitched = null;
        const requestId = resolutionRequestIdOf(e);
        if (requestId !== null) resolutionsByRequestId.set(requestId, resolution);
        carryover = current === null ? [] : openStepsAtEnd(current);
        current = ensureSection(null, `section-after-${e.event_id}`);
        continue;
      }
      if (isNonBoundaryUserMessage(e)) {
        // Collapsed system chips (Stop-hook feedback, browser-fleet nudges,
        // background task-notifications) fold into the current section as a chip
        // rather than opening a new turn. The backend's display decision says
        // which is which; nothing is re-derived here. A chip goes into the skeleton
        // so it both renders at its chronological spot and marks the turn end that
        // ends a step's stint (see collectEjectedProse).
        if (isSystemChipUserMessage(e)) {
          if (current === null) current = ensureSection(null, "section-pre");
          current.entries.push({ kind: "chip", event: e });
        }
        // The other non-boundary messages -- skill expansions and hidden
        // framework injections (/welcome, image notes, resume markers) -- render
        // nowhere on the user rail, so they are dropped here.
        continue;
      }

      // Real user turn: close the prior section (carrying open steps) and open
      // a new one.
      lastSwitched = null;
      carryover = current === null ? [] : openStepsAtEnd(current);
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
      // Apply transitions in transcript order so each step node lands at its
      // real position -- a batched `tk close a && tk start b` must keep a's node
      // before b's. Then route this message's content to the step it belongs
      // to: the step it opened (work shares a message with its `tk start`), or
      // else the step that was current before the message (work shares a
      // message with a `tk close` -- it stays in the closing step).
      const stepBefore = current.current_step_id;
      let lastOpened: string | null = null;
      for (const t of parsed.transitions) {
        // Only step records render in the timeline. A `tk start`/`tk close` on a
        // regular ticket the agent picked up prints the same
        // `Updated <id> -> <status>` line as a step does; skip it (it is neither
        // a `-step-` id nor a known step) rather than mint a phantom node titled
        // with the raw ticket id. Any work batched with that command stays in
        // the step that was already open (or renders inline if none was).
        if (!isStepId(t.id) && !knownSteps.has(t.id)) continue;
        applyTransition(current, t);
        if (t.status === "in_progress") lastOpened = t.id;
      }
      if (parsed.render !== null && (parsed.render.text || parsed.render.tool_calls.length > 0)) {
        if (hasPermissionRequest(parsed.render, toolResults)) {
          // A permission request breaks out of any open step: it must always be
          // directly visible, never collapsed inside a step node. The step stays
          // open (current_step_id is untouched), so work resumed after the user
          // responds keeps grouping under it.
          current.entries.push({ kind: "permission", event: parsed.render });
        } else {
          routeMessage(current, parsed.render, lastOpened ?? stepBefore);
        }
      }
      continue;
    }
    // tool_result events are resolved by id via toolResults; no routing needed.
  }

  // Pending roster: created steps that never transitioned anywhere, in
  // transcript (creation) order.
  const transitioned = new Set<string>();
  for (const b of builders) for (const id of b.step_order) transitioned.add(id);
  const pending: { id: string; title: string }[] = createdOrder
    .filter((id) => !transitioned.has(id))
    .map((id) => ({ id, title: deco.get(id)?.title ?? id }));

  const lastBuilder = builders[builders.length - 1];
  return builders.map((b) =>
    finalizeSection(b, deco, resolutionsByRequestId, b === lastBuilder ? pending : [], agentIsIdle, b === lastBuilder),
  );
}

/** Open (or re-open) a step node as the current step. */
function openStep(section: SectionBuilder, id: string, is_carryover: boolean): void {
  const existing = section.steps.get(id);
  if (existing === undefined) {
    section.steps.set(id, {
      ticket_id: id,
      title: id,
      status: "active",
      summary: null,
      narration: null,
      is_carryover,
      is_frontier: false,
      events: [],
    });
    section.step_order.push(id);
    section.entries.push({ kind: "step", id });
  } else if (existing.status === "done") {
    // Re-opened (a `tk start` on a previously-closed id in this section): it is
    // active again, so it must not keep showing as done/settled.
    existing.status = "active";
    existing.summary = null;
  }
  section.current_step_id = id;
}

function applyTransition(section: SectionBuilder, t: { id: string; status: "in_progress" | "closed" }): void {
  if (t.status === "in_progress") {
    openStep(section, t.id, /* is_carryover */ false);
    return;
  }
  // closed
  const node = section.steps.get(t.id);
  if (node !== undefined) {
    node.status = "done";
  } else {
    // A close with no preceding start in this section (e.g. created and closed
    // before any start was observed): still surface it as a done node.
    section.steps.set(t.id, {
      ticket_id: t.id,
      title: t.id,
      status: "done",
      summary: null,
      narration: null,
      is_carryover: false,
      is_frontier: false,
      events: [],
    });
    section.step_order.push(t.id);
    section.entries.push({ kind: "step", id: t.id });
  }
  if (section.current_step_id === t.id) section.current_step_id = null;
}

function routeMessage(section: SectionBuilder, e: AssistantMessageEvent, step_id: string | null): void {
  section.entries.push({ kind: "event", event: e, step_id });
  if (step_id !== null) {
    section.steps.get(step_id)?.events.push(e);
  }
}

/** Ids of steps still open (active, not done) at the end of a section, in
 *  first-appearance order -- the carryover set. */
function openStepsAtEnd(section: SectionBuilder): string[] {
  return section.step_order.filter((id) => section.steps.get(id)!.status === "active");
}

/** Collect the in-step prose to eject as closing remarks, working stint by
 *  stint.
 *
 *  A *stint* is a run of one step's events uninterrupted by a system chip.
 *  Prose at the end of a stint is a delivered reply, proven by where the chip
 *  sits rather than by what kind of chip it is: an injected user-side line only
 *  arrives at a request boundary, so it either follows a tool result (the stint
 *  ends in work, and there is nothing to eject) or follows prose, which happens
 *  only when that response ended with text and no tool call -- the agent
 *  stopped there. Both halves are visible in any transcript: a text block never
 *  follows a tool_use inside one response, and an injected line never lands
 *  directly after a text-only one. That makes the rule independent of WHEN the
 *  harness delivers a chip, including one it queued while the agent was busy.
 *
 *  Without the split, work the agent does after being woken would retroactively
 *  turn that reply into mid-step narration and bury it inside the collapsed
 *  step -- the step happened to still be open, so the prose stopped being
 *  "after the step's last work".
 *
 *  The live frontier step's LAST stint is exempt: nothing has ended it, so its
 *  trailing prose is in-flight narration shown as a caption (see step 3). Its
 *  earlier stints are not exempt -- a chip ended each of those.
 */
function collectEjectedProse(section: SectionBuilder, frontierId: string | null): Set<string> {
  const stintsByStep = new Map<string, AssistantMessageEvent[][]>();
  for (const id of section.step_order) stintsByStep.set(id, [[]]);
  for (const entry of section.entries) {
    if (entry.kind === "chip") {
      for (const stints of stintsByStep.values()) stints.push([]);
    } else if (entry.kind === "event" && entry.step_id !== null) {
      const stints = stintsByStep.get(entry.step_id);
      if (stints !== undefined) stints[stints.length - 1].push(entry.event);
    }
  }

  const ejected = new Set<string>();
  for (const [id, stints] of stintsByStep) {
    for (let s = 0; s < stints.length; s++) {
      if (id === frontierId && s === stints.length - 1) continue;
      const stint = stints[s];
      let lastWorkIdx = -1;
      for (let i = 0; i < stint.length; i++) if (isWork(stint[i])) lastWorkIdx = i;
      for (let i = lastWorkIdx + 1; i < stint.length; i++) {
        if (isProse(stint[i])) ejected.add(stint[i].event_id);
      }
    }
  }
  return ejected;
}

/** Finalize a section: eject each step's closing prose, pull out the trailing
 *  reply, attribute narration, join decoration, append the pending roster, and
 *  emit items in transcript order. */
function finalizeSection(
  section: SectionBuilder,
  deco: Map<string, Decoration>,
  resolutionsByRequestId: ReadonlyMap<string, PermissionResolution>,
  pending: { id: string; title: string }[],
  agentIsIdle: boolean,
  is_tail: boolean,
): SectionView {
  // The live frontier step (the open step the agent is actively on) -- the only
  // one that may show a spinner. Computed up front because the live step is
  // treated specially below: prose it just spoke is in-flight narration, not a
  // closing remark, since the step has not closed.
  const frontierId = is_tail && !agentIsIdle ? section.current_step_id : null;

  // 1. Ejection: prose spoken inside a step at the end of a stint (so it is NOT
  //    narration, which is prose *followed* by more work in the same stint). It
  //    is the step's closing remark -- ejected from the step so it renders in
  //    the ungrouped inline stream right after the step node, rather than buried
  //    inside it. (If it is the last thing in the section it becomes the
  //    trailing reply below; see step 2.)
  const ejectedIds = collectEjectedProse(section, frontierId);
  for (const id of section.step_order) {
    const node = section.steps.get(id)!;
    node.events = node.events.filter((ev) => !ejectedIds.has(ev.event_id));
  }

  // 2. Trailing reply: the final run of ungrouped prose -- prose entries after
  //    the reply boundary (the latest of the last real work and the last step
  //    appearance). Including the last step appearance keeps a closing remark
  //    promoted to an inline break the moment the next step starts, rather than
  //    sinking below the timeline. These render below the timeline (not inline).
  let lastWorkEntryIdx = -1;
  let lastStepEntryIdx = -1;
  for (let i = 0; i < section.entries.length; i++) {
    const en = section.entries[i];
    // A permission request is real (non-tk) activity, so it acts as a reply
    // boundary just like a work event -- prose before a trailing permission
    // request stays inline at its spot, not hoisted below the timeline as a
    // reply.
    if (en.kind === "permission") lastWorkEntryIdx = i;
    else if (en.kind === "event" && isWork(en.event)) lastWorkEntryIdx = i;
    // A handoff node bounds the reply too: what the agent said before the switch stays above it.
    else if (en.kind === "step" || en.kind === "handoff") lastStepEntryIdx = i;
  }
  const replyBoundary = Math.max(lastWorkEntryIdx, lastStepEntryIdx);
  const trailingIds = new Set<string>();
  const trailing_reply: AssistantMessageEvent[] = [];
  for (let i = replyBoundary + 1; i < section.entries.length; i++) {
    const en = section.entries[i];
    if (en.kind !== "event" || !isProse(en.event)) continue;
    // Prose the agent just spoke inside the live frontier step is that step's
    // in-flight narration (a caption under the still-spinning step), not the
    // turn's wrap-up reply -- the step has not closed, so there is no reply yet.
    // Prose step 1 already ejected is exempt from that: a chip ended the stint
    // it closed, so it is a delivered reply and belongs below the timeline even
    // though the step it came from is still the frontier.
    if (en.step_id !== null && en.step_id === frontierId && !ejectedIds.has(en.event.event_id)) continue;
    trailing_reply.push(en.event);
    trailingIds.add(en.event.event_id);
  }

  // 3. Narration: the latest in-step prose -- the live caption under the step.
  //    For a non-frontier step this is the latest prose followed by more work in
  //    the same step (its trailing closing prose was already ejected in step 1).
  //    For the live frontier step (not ejected) it is simply the last thing the
  //    agent said, so a just-spoken line shows as a caption even before the next
  //    tool call.
  for (const id of section.step_order) {
    const node = section.steps.get(id)!;
    let narration: string | null = null;
    for (const ev of node.events) {
      if (isProse(ev)) narration = ev.text;
    }
    node.narration = narration;
  }

  // 4. Join decoration onto each node. A node with no decoration entry keeps the
  //    raw id as its title and has no summary (its create/tk-step lines scrolled
  //    out of the loaded window -- an accepted loss for very old steps).
  for (const id of section.step_order) {
    const node = section.steps.get(id)!;
    const d = deco.get(id);
    if (d?.title) node.title = d.title;
    if (node.status === "done") node.summary = d?.summary ?? null;
    node.is_frontier = node.ticket_id === frontierId && node.status === "active";
  }

  // 5. Build timeline items by walking the entry skeleton in transcript order.
  //    Each step node is emitted at its first appearance (its transition), so a
  //    step renders at its transcript position even if it carried no work. An
  //    ungrouped event (no step open) or an ejected closing-prose event coalesces
  //    into an inline ungrouped run; an in-step event lives inside its node.
  //    Carryover steps were recorded as the section's first entries, so they
  //    lead the timeline.
  const items: TimelineItem[] = [];
  const emittedSteps = new Set<string>();
  let ungrouped: AssistantMessageEvent[] = [];
  let ungroupedKey = 0;
  const flushUngrouped = (): void => {
    if (ungrouped.length > 0) {
      items.push({ kind: "ungrouped", key: `${section.key}-ung-${ungroupedKey++}`, events: ungrouped });
      ungrouped = [];
    }
  };

  for (const entry of section.entries) {
    if (entry.kind === "step") {
      flushUngrouped();
      if (!emittedSteps.has(entry.id)) {
        items.push({ kind: "step", step: section.steps.get(entry.id)! });
        emittedSteps.add(entry.id);
      }
    } else if (entry.kind === "permission") {
      // A permission break ends any in-flight ungrouped run and stands as its
      // own always-visible item at its transcript position. Each card looks up
      // its own verdict in resolutionsByRequestId by its own request id.
      flushUngrouped();
      items.push({ kind: "permission", event: entry.event, resolutionsByRequestId });
    } else if (entry.kind === "chip") {
      // Likewise a chip: it ends any in-flight ungrouped run and stands at its
      // own transcript position.
      flushUngrouped();
      items.push({ kind: "chip", event: entry.event });
    } else if (entry.kind === "handoff") {
      flushUngrouped();
      items.push({ kind: "handoff", node: entry.node });
    } else if (trailingIds.has(entry.event.event_id)) {
      // Trailing reply: rendered below the timeline (see trailing_reply).
    } else if (entry.step_id === null || ejectedIds.has(entry.event.event_id)) {
      // An ungrouped event (no step open) or a step's ejected closing prose:
      // coalesce into an inline run. In-step events render inside the step node.
      ungrouped.push(entry.event);
    }
  }
  flushUngrouped();

  // 6. Pending roster (tail section only): created steps that never started, as
  //    dashed placeholders at the tail, in transcript order.
  if (is_tail) {
    for (const p of pending) {
      items.push({
        kind: "step",
        step: {
          ticket_id: p.id,
          title: p.title,
          status: "pending",
          summary: null,
          narration: null,
          is_carryover: false,
          is_frontier: false,
          events: [],
        },
      });
    }
  }

  return { user_event: section.user_event, key: section.key, items, trailing_reply };
}
