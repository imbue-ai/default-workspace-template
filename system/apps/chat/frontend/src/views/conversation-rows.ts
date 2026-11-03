/**
 * Shared row builder for the main chat and a subagent's conversation.
 *
 * Both views render the same way: a single in-order walk of the transcript
 * (buildSections) into turn sections, flattened into the virtualized list's
 * top-level rows (buildRows) -- a user message, a whole ProgressBlock for a turn
 * that has tk steps, an ungrouped assistant message, a stop-hook chip, a handoff
 * node, a trailing wrap-up reply, or the chip row of a compaction that followed
 * it. Sharing it here means a subagent's "View conversation"
 * gets the real progress view -- step timeline, statuses, summaries -- and the
 * same windowed virtualization as the main chat, with zero rendering drift.
 *
 * Every row of the agent's output sits in the agent's column, and the first row
 * of each stretch of it wears the agent's name and face.
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
import type { TranscriptEvent, ToolResultEvent } from "../models/Response";
import {
  renderUserMessage,
  renderAssistantMessage,
  renderAssistantRunRow,
  isChipOnlyEvent,
  renderPermissionRow,
  buildToolResultsWithSkillExpansions,
  type RunPart,
} from "./message-renderers";
import { computeAuthErrorHiddenEventIds } from "./auth-recovery";
import { isHiddenUserMessage } from "./message-classification";
import type { CompactionPart } from "./compaction-chips";
import { buildSections, isGenuineUserTurn, type LiveCompaction, type SectionView } from "./turn-grouping";
import { ProgressBlock } from "./ProgressBlock";
import { renderHandoffRow } from "./handoff-node";
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
  // global event index (scroll persistence and fill-planner focus). Null only
  // for a turn section with no opening user message; consumers fall back to the
  // previous row's event.
  anchorEventId: string | null;
  // m.Children (not m.Vnode) because a row can be a component vnode
  // (ProgressBlock), whose typed attrs do not fit the bare Vnode<{}, {}>.
  render: () => m.Children;
  /** Whether the stretch of the agent's output this row leaves open has worn the header already, so a row of the
   *  agent's that follows goes without it. */
  isAgentHeaderShown: boolean;
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
): RowDescriptor[] {
  const rows: RowDescriptor[] = [];
  // A stretch of the agent's output runs until a person speaks or another agent takes over; system chips and
  // notices between its rows do not end it.
  let isAgentHeaderShown = false;
  const pushRow = (row: Omit<RowDescriptor, "isAgentHeaderShown">): void => {
    rows.push({ ...row, isAgentHeaderShown });
  };
  const pushAgentRow = (
    row: Omit<RowDescriptor, "render" | "isAgentHeaderShown">,
    render: (isHeaded: boolean) => m.Children,
  ): void => {
    const isHeaded = !isAgentHeaderShown;
    isAgentHeaderShown = true;
    rows.push({ ...row, render: () => render(isHeaded), isAgentHeaderShown });
  };
  // The compactions that landed after a turn's reply: one chip row of their own below it.
  const pushTrailingCompactionRow = (parts: CompactionPart[]): void => {
    if (parts.length === 0) return;
    pushAgentRow(
      { key: parts[0].event_id, estimate: ESTIMATED_CHIP_ROW_HEIGHT_PX, anchorEventId: runAnchorEventId(parts[0]) },
      (isHeaded) => renderAssistantRunRow(parts, toolResults, chatId, isHeaded),
    );
  };
  for (const section of sections) {
    if (section.is_after_switch) {
      // The new agent's stretch is open past the last row even before it has a row of its own, so the reply it is
      // still writing (ChatPanel's tail) wears the header.
      isAgentHeaderShown = false;
      if (rows.length > 0) rows[rows.length - 1].isAgentHeaderShown = false;
    }
    const userEvent = section.user_event;
    if (userEvent !== null && !isHiddenUserMessage(userEvent)) {
      if (isGenuineUserTurn(userEvent)) isAgentHeaderShown = false;
      pushRow({
        key: userEvent.event_id,
        estimate: ESTIMATED_USER_HEIGHT_PX,
        anchorEventId: userEvent.event_id,
        render: () => renderUserMessage(userEvent, resentTurnIds.has(userEvent.event_id)) as m.Vnode,
      });
    }

    const hasSteps = section.items.some((i) => i.kind === "step");
    if (hasSteps) {
      const key = `progress-${section.key}`;
      pushAgentRow(
        { key, estimate: ESTIMATED_PROGRESS_HEIGHT_PX, anchorEventId: userEvent?.event_id ?? null },
        (isHeaded) =>
          m(ProgressBlock, {
            id: key,
            key,
            items: section.items,
            trailing_reply: section.trailing_reply,
            toolResults,
            chatId,
            isHeaded,
          }),
      );
      pushTrailingCompactionRow(section.trailing_status);
      continue;
    }

    // No steps this turn: render the body as plain chat -- prose and tool-call
    // blocks inline, the same as assistant messages outside a progress section.
    for (const item of section.items) {
      if (item.kind === "ungrouped") {
        // A row is a RUN: one head event plus every chip-only event that
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
            pushAgentRow(
              { key: head.event_id, estimate: ESTIMATED_ASSISTANT_HEIGHT_PX, anchorEventId: head.event_id },
              (isHeaded) => renderAssistantMessage(head, toolResults, chatId, isHeaded),
            );
            continue;
          }
          // Keyed and anchored on the run's head: the row stands where that
          // event does in the transcript, which is what scroll persistence and
          // the fill planner resolve a row by.
          pushAgentRow(
            {
              key: head.event_id,
              estimate: isChipOnlyEvent(head, toolResults)
                ? ESTIMATED_CHIP_ROW_HEIGHT_PX
                : ESTIMATED_ASSISTANT_HEIGHT_PX,
              anchorEventId: runAnchorEventId(head),
            },
            (isHeaded) => renderAssistantRunRow(run, toolResults, chatId, isHeaded),
          );
        }
      } else if (item.kind === "permission") {
        // A permission request lifted out of its step: rendered inline as an
        // always-visible card so the user can act on it without expanding a step.
        const permissionEvent = item.event;
        const resolutionsByRequestId = item.resolutionsByRequestId;
        const secretNotesByRequestId = item.secretNotesByRequestId;
        const permKey = `perm-${permissionEvent.event_id}`;
        pushAgentRow(
          { key: permKey, estimate: ESTIMATED_ASSISTANT_HEIGHT_PX, anchorEventId: permissionEvent.event_id },
          (isHeaded) =>
            renderPermissionRow(
              permissionEvent,
              toolResults,
              chatId,
              permKey,
              isHeaded,
              resolutionsByRequestId,
              secretNotesByRequestId,
            ),
        );
      } else if (item.kind === "chip") {
        const chipEvent = item.event;
        if (!isHiddenUserMessage(chipEvent)) {
          pushRow({
            key: chipEvent.event_id,
            estimate: ESTIMATED_USER_HEIGHT_PX,
            anchorEventId: chipEvent.event_id,
            render: () => renderUserMessage(chipEvent) as m.Vnode,
          });
        }
      } else if (item.kind === "handoff") {
        // The handoff node as a row of its own. The row's id is the node's key so the
        // virtualized list can measure it.
        const node = item.node;
        const key = `handoff-${node.key}`;
        pushAgentRow(
          {
            key,
            estimate: ESTIMATED_USER_HEIGHT_PX,
            anchorEventId: node.request?.event_id ?? node.switch?.event_id ?? null,
          },
          (isHeaded) => renderHandoffRow(node, chatId, toolResults, { key, id: key, expansionKey: key, isHeaded }),
        );
      }
    }
    for (const event of section.trailing_reply) {
      pushAgentRow(
        { key: event.event_id, estimate: ESTIMATED_ASSISTANT_HEIGHT_PX, anchorEventId: event.event_id },
        (isHeaded) => renderAssistantMessage(event, toolResults, chatId, isHeaded),
      );
    }
    pushTrailingCompactionRow(section.trailing_status);
  }
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
 * second time and folds their second copies into it.
 */
export function buildConversationRows(
  chatId: string,
  events: TranscriptEvent[],
  isTailSettled: boolean,
  liveCompaction: LiveCompaction | null = null,
  resentTurns: ResentTurns | null = null,
): RowDescriptor[] {
  const toolResults = buildToolResultsWithSkillExpansions(events);
  const hiddenEventIds = computeAuthErrorHiddenEventIds(events);
  for (const eventId of resentTurns?.foldedTurnIds ?? []) hiddenEventIds.add(eventId);
  const visibleEvents = hiddenEventIds.size > 0 ? events.filter((e) => !hiddenEventIds.has(e.event_id)) : events;
  const sections = buildSections(visibleEvents, toolResults, isTailSettled, liveCompaction);
  return buildRows(chatId, sections, toolResults, resentTurns?.notedTurnIds ?? new Set());
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
