/**
 * Shared row builder for the main chat and a subagent's conversation.
 *
 * Both views render the same way: a single in-order walk of the transcript
 * (buildSections) into turn sections, flattened into the virtualized list's
 * top-level rows (buildRows): a person's message, or a stretch of the agent's
 * output -- everything from the first thing it does after a person speaks until
 * the next person speaks or another agent takes over -- under the agent's name
 * and face, threaded by one line (see machine-bubbles). A stretch's parts are a
 * whole ProgressBlock for a turn that has tk steps, an ungrouped assistant
 * message, a system chip or notice, a handoff node, a trailing wrap-up reply, or
 * the chip row of a compaction that followed it. Sharing it here means a subagent's "View
 * conversation" gets the real progress view -- step timeline, statuses,
 * summaries -- and the same windowed virtualization as the main chat, with zero
 * rendering drift.
 *
 * Structure and decoration both come from the transcript walk (tk prints its
 * step decoration on stdout, which buildSections parses); there is no
 * side-channel enrichment. `isTailSettled` settles the frontier spinner on the
 * tail turn (see `isTailTurnSettled`), and `liveCompaction` places the start
 * chip of a compaction still running (see `liveCompactionOf`). A message a
 * sign-in failure refused, and the failure, are hidden here once it was sent
 * again right after the failure (a no-op for a subagent, which never has one)
 * so the two views stay byte-identical.
 */

import m from "mithril";
import type { AssistantMessageEvent, TranscriptEvent, ToolResultEvent, UserMessageEvent } from "../models/Response";
import {
  renderUserMessage,
  renderAssistantMessage,
  renderAssistantRunRow,
  isChipOnlyEvent,
  renderPermissionItem,
  rendersAsChip,
  buildToolResultsWithSkillExpansions,
  type RunPart,
} from "./message-renderers";
import { computeAuthErrorHiddenEventIds } from "./auth-recovery";
import { isHiddenUserMessage, isPersonsChipMessage } from "./message-classification";
import type { CompactionPart } from "./compaction-chips";
import { buildSections, isGenuineUserTurn, type LiveCompaction, type SectionView } from "./turn-grouping";
import { ProgressBlock } from "./ProgressBlock";
import { renderFramedHandoffNode } from "./handoff-node";
import { renderAgentTurn } from "./machine-bubbles";
import type { ResentTurns } from "../models/ResentSends";

// Per-type fallback row heights, used until a row has been measured (live or
// offscreen). Rough is fine: they only affect spacer sizing for not-yet-measured
// rows, which the measurement passes correct.
export const ESTIMATED_USER_HEIGHT_PX = 90;
export const ESTIMATED_ASSISTANT_HEIGHT_PX = 240;
export const ESTIMATED_PROGRESS_HEIGHT_PX = 360;
// A merged chip row is one line of chips plus the message gap below it -- an
// order of magnitude shorter than the prose message the assistant estimate is
// sized for.
export const ESTIMATED_CHIP_ROW_HEIGHT_PX = 48;

// Layout for the centered message column. Shared by the live transcript views
// and the offscreen measurer, whose rows must lay out identically to measure
// identically.
export const MESSAGE_LIST_CLASS = "message-list mx-auto w-full max-w-(--width-message-column) flex flex-col py-6";

export interface RowDescriptor {
  key: string;
  estimate: number;
  // The transcript event this row starts at, for mapping a row anchor to a
  // global event index (scroll persistence and fill-planner focus): a stretch's
  // first part that has one. Null when none of its parts does (the compaction
  // running now, a step block whose section opened with no user message);
  // consumers fall back to the previous row's event.
  anchorEventId: string | null;
  render: () => m.Children;
  /** Whether the agent's output past the rows continues this row's stretch: the last row, when it is a stretch of
   *  the agent's output that no switch to another agent has ended. */
  takesLiveTail: boolean;
  /** The calls this row shows as chips on its stretch's line (not those inside a step's work), so the page can tell
   *  whether the call the agent is running shows there. Empty for a person's row. */
  chipCallIds: ReadonlySet<string>;
  /** The person's turn a person's row shows, which the next person's turn may carry on the run of; null for a stretch
   *  of the agent's output and for a bash-mode command. */
  speakerTurn: UserMessageEvent | null;
}

/** The agent's output past the transcript's rows (a handoff the transcript does not show yet, the reply still being
 *  written, the status row), read when the row it lands in renders. */
export type LiveTail = () => m.Children[];

/** One piece of a stretch of the agent's output: a message, a chip run, a turn's progress block, a card, a notice. */
interface TurnPart {
  key: string;
  estimate: number;
  anchorEventId: string | null;
  /** `isStretchHead` when the part opens its stretch. */
  render: (isStretchHead: boolean) => m.Children;
  chipCallIds: readonly string[];
}

/** The calls among `events` that show as chips. */
function chipCallIdsOf(events: readonly RunPart[], toolResults: Map<string, ToolResultEvent>): string[] {
  return events.flatMap((event) =>
    event.type === "compaction"
      ? []
      : event.tool_calls.filter((call) => rendersAsChip(call, toolResults)).map((call) => call.tool_call_id),
  );
}

/** A stretch of the agent's output as one row: its parts under the agent's name and face. */
export function renderAgentTurnRow(key: string, parts: m.Children[]): m.Vnode {
  return m(
    "div",
    { id: key, key, class: "message-agent-turn mb-5" },
    renderAgentTurn(parts.filter((part) => part !== null && part !== undefined)),
  );
}

/** A run of consecutive rows to render, `[startIndex, endIndex)`, or a spacer
 *  standing in for everything a run omits (including the virtual end spacers). */
export type WindowSegment =
  { kind: "rows"; startIndex: number; endIndex: number } | { kind: "spacer"; height: number };

/**
 * Render the ordered window segments (from the scroll engine's render plan) into
 * the message list's children: a spacer div for each spacer, and each row's own
 * vnode for each row-run. Shared by ChatPanel and SubagentView so both
 * virtualize identically. Spacer keys are role-stable (top/mid/bottom).
 */
export function renderTranscriptSegments(rows: RowDescriptor[], segments: WindowSegment[]): m.Children[] {
  const children: m.Children[] = [];
  for (let s = 0; s < segments.length; s++) {
    const segment = segments[s];
    if (segment.kind === "spacer") {
      const role = s === 0 ? "top" : s === segments.length - 1 ? "bottom" : "mid";
      children.push(
        m("div", { key: `__spacer_${role}`, style: `height: ${segment.height}px; overflow-anchor: none` }),
      );
    } else {
      for (let i = segment.startIndex; i < segment.endIndex; i++) {
        children.push(rows[i].render());
      }
    }
  }
  return children;
}

/**
 * Flatten the turn-grouped sections into the virtualized list's top-level rows.
 *
 * Each row is one mounted node in the message list. Keeping the grouping here
 * (rather than virtualizing raw events) preserves turn structure, the progress
 * timeline, skill expansions and auth-error hiding while still mounting only the
 * windowed rows. Render closures are invoked lazily so off-window rows never
 * build their vnodes (so MarkdownContent is only parsed for on-screen rows).
 * Every row's rendered root carries a DOM ``id`` equal to its ``key`` so
 * measureRows can read its height.
 */
function buildRows(
  chatId: string,
  sections: SectionView[],
  toolResults: Map<string, ToolResultEvent>,
  resentTurnIds: ReadonlySet<string>,
  replyDraft: (() => AssistantMessageEvent | null) | null,
  liveTail: LiveTail | null,
): RowDescriptor[] {
  const rows: RowDescriptor[] = [];
  // A stretch of the agent's output runs until a person speaks or another agent takes over; the system chips and
  // notices among it are part of it.
  let parts: TurnPart[] = [];
  // The person's turn on the last row, when the last row is one: the next person's turn may carry on its run.
  let previousPersonTurn: UserMessageEvent | null = null;
  // Whether the agent's output past the rows would extend the last stretch: false once a person speaks or another
  // agent takes over, until that agent's first part.
  let isLastTurnOpen = false;
  const endTurn = (): void => {
    isLastTurnOpen = false;
    if (parts.length === 0) return;
    const turnParts = parts;
    parts = [];
    const key = `turn-${turnParts[0].key}`;
    const row: RowDescriptor = {
      key,
      estimate: turnParts.reduce((sum, part) => sum + part.estimate, 0),
      anchorEventId: turnParts.find((part) => part.anchorEventId !== null)?.anchorEventId ?? null,
      render: () => {
        const tail = liveTail !== null && row.takesLiveTail ? liveTail() : [];
        return renderAgentTurnRow(key, [...turnParts.map((part, index) => part.render(index === 0)), ...tail]);
      },
      takesLiveTail: false,
      chipCallIds: new Set(turnParts.flatMap((part) => part.chipCallIds)),
      speakerTurn: null,
    };
    rows.push(row);
    previousPersonTurn = null;
  };
  const addPart = (part: TurnPart): void => {
    parts.push(part);
    isLastTurnOpen = true;
  };
  // The compactions that landed after a turn's reply: a chip run of their own below it.
  const addTrailingCompactions = (compactions: CompactionPart[]): void => {
    if (compactions.length === 0) return;
    addPart({
      key: compactions[0].event_id,
      estimate: ESTIMATED_CHIP_ROW_HEIGHT_PX,
      anchorEventId: runAnchorEventId(compactions[0]),
      render: (isStretchHead) => renderAssistantRunRow(compactions, toolResults, chatId, isStretchHead),
      chipCallIds: [],
    });
  };
  for (const section of sections) {
    if (section.is_after_switch) endTurn();
    const userEvent = section.user_event;
    if (userEvent !== null && !isHiddenUserMessage(userEvent)) {
      if (isGenuineUserTurn(userEvent) || isPersonsChipMessage(userEvent)) {
        endTurn();
        const previousTurn = previousPersonTurn;
        // A bash-mode command is the person's, but not words of theirs: it breaks the run of their turns.
        const speakerTurn = isPersonsChipMessage(userEvent) ? null : userEvent;
        rows.push({
          key: userEvent.event_id,
          estimate: ESTIMATED_USER_HEIGHT_PX,
          anchorEventId: userEvent.event_id,
          render: () => renderUserMessage(userEvent, resentTurnIds.has(userEvent.event_id), previousTurn) as m.Vnode,
          takesLiveTail: false,
          chipCallIds: new Set(),
          speakerTurn,
        });
        previousPersonTurn = speakerTurn;
      } else {
        addPart({
          key: userEvent.event_id,
          estimate: ESTIMATED_USER_HEIGHT_PX,
          anchorEventId: userEvent.event_id,
          render: () => renderUserMessage(userEvent),
          chipCallIds: [],
        });
      }
    }

    const hasSteps = section.items.some((i) => i.kind === "step");
    if (hasSteps) {
      const key = `progress-${section.key}`;
      addPart({
        key,
        estimate: ESTIMATED_PROGRESS_HEIGHT_PX,
        anchorEventId: userEvent?.event_id ?? null,
        render: (isStretchHead) =>
          m(ProgressBlock, {
            id: key,
            key,
            items: section.items,
            trailing_reply: section.trailing_reply,
            toolResults,
            chatId,
            isStretchHead,
            liveStepId: section.live_step_id,
            replyDraft: replyDraft ?? undefined,
          }),
        // The calls on the timeline's own line: those of its ungrouped runs, its permission requests' messages and
        // its wrap-up, not a step's.
        chipCallIds: chipCallIdsOf(
          [
            ...section.items.flatMap((item) =>
              item.kind === "ungrouped" ? item.events : item.kind === "permission" ? [item.event] : [],
            ),
            ...section.trailing_reply,
          ],
          toolResults,
        ),
      });
      addTrailingCompactions(section.trailing_status);
      continue;
    }

    // No steps this turn: render the body as plain chat -- prose and tool-call
    // blocks inline, the same as assistant messages outside a progress section.
    for (const item of section.items) {
      if (item.kind === "ungrouped") {
        // A part is a RUN: one head event plus every chip-only event that
        // follows it, so a turn the harness split across several events lays
        // out as the one message it is (see renderAssistantRunRow).
        let i = 0;
        while (i < item.events.length) {
          const start = i;
          // The head joins unconditionally; only chip-only events accrete onto
          // it, since anything else is a message in its own right.
          i++;
          while (i < item.events.length && isChipOnlyEvent(item.events[i], toolResults)) {
            i++;
          }
          const run = item.events.slice(start, i);
          const head = run[0];
          // A lone event that is not a chip run keeps the memoized single-message
          // renderer -- nothing was merged, so there is nothing to gain by
          // giving up the memo.
          if (run.length === 1 && head.type === "assistant_message" && !isChipOnlyEvent(head, toolResults)) {
            addPart({
              key: head.event_id,
              estimate: ESTIMATED_ASSISTANT_HEIGHT_PX,
              anchorEventId: head.event_id,
              render: (isStretchHead) => renderAssistantMessage(head, toolResults, chatId, isStretchHead),
              chipCallIds: chipCallIdsOf(run, toolResults),
            });
            continue;
          }
          addPart({
            key: head.event_id,
            estimate: isChipOnlyEvent(head, toolResults)
              ? ESTIMATED_CHIP_ROW_HEIGHT_PX
              : ESTIMATED_ASSISTANT_HEIGHT_PX,
            anchorEventId: runAnchorEventId(head),
            render: (isStretchHead) => renderAssistantRunRow(run, toolResults, chatId, isStretchHead),
            chipCallIds: chipCallIdsOf(run, toolResults),
          });
        }
      } else if (item.kind === "permission") {
        // A permission request lifted out of its step: rendered inline as an
        // always-visible card so the user can act on it without expanding a step.
        const permissionEvent = item.event;
        const resolutionsByRequestId = item.resolutionsByRequestId;
        const secretNotesByRequestId = item.secretNotesByRequestId;
        addPart({
          key: `perm-${permissionEvent.event_id}`,
          estimate: ESTIMATED_ASSISTANT_HEIGHT_PX,
          anchorEventId: permissionEvent.event_id,
          render: (isStretchHead) =>
            renderPermissionItem(
              permissionEvent,
              toolResults,
              chatId,
              resolutionsByRequestId,
              secretNotesByRequestId,
              isStretchHead,
            ),
          chipCallIds: chipCallIdsOf([permissionEvent], toolResults),
        });
      } else if (item.kind === "chip") {
        const chipEvent = item.event;
        if (!isHiddenUserMessage(chipEvent)) {
          addPart({
            key: chipEvent.event_id,
            estimate: ESTIMATED_USER_HEIGHT_PX,
            anchorEventId: chipEvent.event_id,
            render: () => renderUserMessage(chipEvent),
            chipCallIds: [],
          });
        }
      } else if (item.kind === "handoff") {
        const node = item.node;
        const key = `handoff-${node.key}`;
        addPart({
          key,
          estimate: ESTIMATED_USER_HEIGHT_PX,
          anchorEventId: node.request?.event_id ?? node.switch?.event_id ?? null,
          render: () => renderFramedHandoffNode(node, chatId, toolResults, { key, expansionKey: key }),
          chipCallIds: [],
        });
      }
    }
    for (const event of section.trailing_reply) {
      addPart({
        key: event.event_id,
        estimate: ESTIMATED_ASSISTANT_HEIGHT_PX,
        anchorEventId: event.event_id,
        render: (isStretchHead) => renderAssistantMessage(event, toolResults, chatId, isStretchHead),
        chipCallIds: chipCallIdsOf([event], toolResults),
      });
    }
    addTrailingCompactions(section.trailing_status);
  }
  const isTailOpen = isLastTurnOpen;
  endTurn();
  if (isTailOpen) rows[rows.length - 1].takesLiveTail = true;
  return rows;
}

/** The transcript event a run's row stands at: its head's, or none for the compaction
 *  running now, which has no event yet (the row then resolves by the row before it). */
function runAnchorEventId(head: RunPart): string | null {
  return head.type === "compaction" && head.event === null ? null : head.event_id;
}

/**
 * The full events -> virtualized rows pipeline shared by both conversation
 * views: hide the refused messages that were sent again (and their sign-in
 * failures), walk the transcript into turn
 * sections, then flatten into top-level rows. The structure and decoration --
 * which steps exist, their order, grouping, titles, summaries -- come purely
 * from the transcript walk. ``resentTurns`` (the main chat's own, see
 * models/ResentSends) puts "Sent again" under the messages the chat app sent a
 * second time and folds their second copies into it. ``liveTail`` is the agent's
 * output past the transcript, drawn at the end of the last row when that row is a
 * stretch of the agent's output.
 */
export function buildConversationRows(
  chatId: string,
  events: TranscriptEvent[],
  isTailSettled: boolean,
  liveCompaction: LiveCompaction | null = null,
  resentTurns: ResentTurns | null = null,
  liveTail: LiveTail | null = null,
): RowDescriptor[] {
  return buildConversationView(chatId, events, isTailSettled, liveCompaction, resentTurns, null, liveTail).rows;
}

/** The rows, and whether the reply the agent is still writing shows inside them. */
export interface ConversationView {
  rows: RowDescriptor[];
  /** True when a step is live at the tail: the draft shows in it (read at draw time through
   *  ``replyDraft``), as the words would once they land, rather than in the live tail. */
  isReplyDraftInStep: boolean;
}

/** ``buildConversationRows`` for a chat that streams a reply draft (see models/ReplyDrafts). */
export function buildConversationView(
  chatId: string,
  events: TranscriptEvent[],
  isTailSettled: boolean,
  liveCompaction: LiveCompaction | null,
  resentTurns: ResentTurns | null,
  replyDraft: (() => AssistantMessageEvent | null) | null,
  liveTail: LiveTail | null = null,
): ConversationView {
  const toolResults = buildToolResultsWithSkillExpansions(events);
  const hiddenEventIds = computeAuthErrorHiddenEventIds(events);
  for (const eventId of resentTurns?.foldedTurnIds ?? []) hiddenEventIds.add(eventId);
  const visibleEvents = hiddenEventIds.size > 0 ? events.filter((e) => !hiddenEventIds.has(e.event_id)) : events;
  const sections = buildSections(visibleEvents, toolResults, isTailSettled, liveCompaction);
  return {
    rows: buildRows(chatId, sections, toolResults, resentTurns?.notedTurnIds ?? new Set(), replyDraft, liveTail),
    isReplyDraftInStep: sections.some((section) => section.live_step_id !== null),
  };
}

/**
 * Whether a subagent is still running, used in place of the parent agent's
 * server-derived `activity_state` (which doesn't apply to a subagent). Minimal
 * by design: the subagent is running while its last assistant turn has no
 * terminal stop_reason (it's mid-tool-use or hasn't stopped); once it stops
 * with `end_turn`/`stop_sequence` it's settled. Drives whether the subagent's
 * frontier step may show a spinner.
 */
export function isSubagentRunning(events: TranscriptEvent[]): boolean {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.type === "assistant_message") {
      return event.stop_reason === null || event.stop_reason === "tool_use";
    }
  }
  return false;
}
