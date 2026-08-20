/**
 * Shared row builder for the main chat and a subagent's conversation.
 *
 * Both views render the same way: a single in-order walk of the transcript
 * (buildSections) into turn sections, flattened into the virtualized list's
 * top-level rows (buildRows) -- a user message, a whole ProgressBlock for a turn
 * that has tk steps, an ungrouped assistant message, a stop-hook chip, or a
 * trailing wrap-up reply. Sharing it here means a subagent's "View conversation"
 * gets the real progress view -- step timeline, statuses, summaries -- and the
 * same windowed virtualization as the main chat, with zero rendering drift.
 *
 * Structure and decoration both come from the transcript walk (tk prints its
 * step decoration on stdout, which buildSections parses); there is no
 * side-channel enrichment. `agentIsIdle` settles the frontier spinner on the
 * tail turn. The pre-login auth-error prefix is hidden here (a no-op for a
 * subagent, which never has one) so the two views stay byte-identical.
 */

import m from "mithril";
import type { TranscriptEvent, ToolResultEvent } from "../models/Response";
import {
  renderUserMessage,
  renderAssistantMessage,
  renderPermissionItem,
  buildToolResultsWithSkillExpansions,
  computeAuthErrorHiddenEventIds,
} from "./message-renderers";
import { isHiddenUserMessage } from "./message-classification";
import { buildSections, type SectionView } from "./turn-grouping";
import { ProgressBlock } from "./ProgressBlock";
import type { VirtualItem } from "@tanstack/virtual-core";

// Fallback heights, used only until a row has been measured. Rough is fine: they
// affect spacer sizing for off-screen rows, which is corrected the moment a row
// scrolls into view and is measured.
const ESTIMATED_USER_HEIGHT_PX = 90;
const ESTIMATED_ASSISTANT_HEIGHT_PX = 240;
const ESTIMATED_PROGRESS_HEIGHT_PX = 360;

export interface RowDescriptor {
  key: string;
  estimate: number;
  /**
   * The global transcript event range this row renders, as [start, end).
   *
   * Carried so a measured height can be attributed to the events it covers,
   * which is what lets reserved space for unloaded history be sized from real
   * geometry instead of a per-event guess. Ranges are made contiguous across the
   * loaded window (see makeRangesContiguous), so events that render nothing --
   * a hidden user message, an auth-error prefix -- are absorbed by the row above
   * them rather than leaving a gap that would be estimated as if it had height.
   */
  start_offset: number;
  end_offset: number;
  // m.Children (not m.Vnode) because a row can be a component vnode
  // (ProgressBlock), whose typed attrs do not fit the bare Vnode<{}, {}>.
  render: () => m.Children;
}

/**
 * Render the virtualizer's items into the message list's children: each row's
 * own vnode, with spacers standing in for everything not rendered. Shared by
 * ChatPanel and SubagentView so both virtualize identically.
 *
 * The items carry their own offsets, so a gap between consecutive items means
 * the rendered set is deliberately disjoint -- the selection pin holding rows
 * far from the viewport. That gap becomes one spacer, so a selection survives at
 * any scroll distance without mounting everything between it and the viewport.
 */
export function renderVirtualRows(rows: RowDescriptor[], items: VirtualItem[], trailingSpace: number): m.Children[] {
  const children: m.Children[] = [];
  if (items.length === 0) {
    return children;
  }
  // Leading spacer: the first rendered row's own offset, which already includes
  // any reserved space for unloaded history above the window.
  children.push(spacer("top", items[0].start));
  let previousEnd = items[0].start;
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    // A gap means the rendered set is disjoint -- the selection pin is holding
    // rows far from the viewport. Bridge it with one spacer rather than mounting
    // the arbitrarily many rows in between.
    if (item.start > previousEnd) {
      children.push(spacer(`mid_${i}`, item.start - previousEnd));
    }
    const row = rows[item.index];
    if (row !== undefined) {
      children.push(row.render());
    }
    previousEnd = item.end;
  }
  children.push(spacer("bottom", trailingSpace));
  return children;
}

/**
 * Spacers carry `overflow-anchor: none` so the browser's scroll anchoring never
 * picks one -- their heights change as rows page in and measure -- and anchors
 * to a real message row instead.
 *
 * The two that always exist are keyed by role, so they are patched in place
 * rather than rebuilt. A gap bridging a disjoint selection pin is keyed by its
 * position in the rendered set instead, so two gaps in one render cannot
 * collide; that key moves as the window scrolls, which costs nothing because
 * the node it keys is an empty div.
 */
function spacer(role: string, height: number): m.Children {
  return m("div", { key: `__spacer_${role}`, style: `height: ${Math.max(0, height)}px; overflow-anchor: none` });
}

/**
 * The first transcript event a section's *body* renders, used to anchor the
 * section's progress row.
 *
 * Deliberately skips `user_event`: that message is its own row above the
 * progress block, so anchoring here would give both rows the same offset and
 * leave the user row covering nothing. The user event is only used as a last
 * resort, for a section whose body turns out to hold no events at all.
 */
function firstEventIdOfSection(section: SectionView): string | undefined {
  for (const item of section.items) {
    if (item.kind === "step") {
      if (item.step.events.length > 0) {
        return item.step.events[0].event_id;
      }
    } else if (item.kind === "ungrouped") {
      if (item.events.length > 0) {
        return item.events[0].event_id;
      }
    } else {
      return item.event.event_id;
    }
  }
  return section.trailing_reply[0]?.event_id ?? section.user_event?.event_id;
}

/**
 * Flatten the turn-grouped sections into the virtualized list's top-level rows.
 *
 * Each row is one mounted node in the message list. Keeping the grouping here
 * (rather than virtualizing raw events) preserves turn structure, the progress
 * timeline, skill expansions and auth-error hiding while still mounting only the
 * windowed rows. Render closures are invoked lazily so off-window rows never
 * build their vnodes (so MarkdownContent is only parsed for on-screen rows).
 * Every row's rendered root carries a DOM ``id`` equal to its ``key``, which is
 * how both the measurement pass and the selection code find it.
 */
function buildRows(
  agentId: string,
  sections: SectionView[],
  toolResults: Map<string, ToolResultEvent>,
  offsetOf: (eventId: string | undefined) => number,
): RowDescriptor[] {
  const rows: RowDescriptor[] = [];
  // end_offset is filled in by makeRangesContiguous once every row is known;
  // until then a row only claims where it starts.
  const push = (row: Omit<RowDescriptor, "end_offset">): void => {
    rows.push({ ...row, end_offset: row.start_offset });
  };

  for (const section of sections) {
    const userEvent = section.user_event;
    if (userEvent !== null && !isHiddenUserMessage(userEvent)) {
      push({
        key: userEvent.event_id,
        estimate: ESTIMATED_USER_HEIGHT_PX,
        start_offset: offsetOf(userEvent.event_id),
        render: () => renderUserMessage(userEvent) as m.Vnode,
      });
    }

    const hasSteps = section.items.some((i) => i.kind === "step");
    if (hasSteps) {
      const key = `progress-${section.key}`;
      push({
        key,
        estimate: ESTIMATED_PROGRESS_HEIGHT_PX,
        start_offset: offsetOf(firstEventIdOfSection(section)),
        render: () =>
          m(ProgressBlock, {
            id: key,
            key,
            items: section.items,
            trailing_reply: section.trailing_reply,
            toolResults,
            agentId,
          }),
      });
      continue;
    }

    // No steps this turn: render the body as plain chat -- prose and tool-call
    // blocks inline, the same as assistant messages outside a progress section.
    for (const item of section.items) {
      if (item.kind === "ungrouped") {
        for (const event of item.events) {
          push({
            key: event.event_id,
            estimate: ESTIMATED_ASSISTANT_HEIGHT_PX,
            start_offset: offsetOf(event.event_id),
            render: () => renderAssistantMessage(event, toolResults, agentId),
          });
        }
      } else if (item.kind === "permission") {
        // A permission request lifted out of its step: rendered inline as an
        // always-visible card so the user can act on it without expanding a step.
        const permissionEvent = item.event;
        const resolution = item.resolution;
        const permKey = `perm-${permissionEvent.event_id}`;
        push({
          key: permKey,
          estimate: ESTIMATED_ASSISTANT_HEIGHT_PX,
          start_offset: offsetOf(permissionEvent.event_id),
          // Pass the row key as the DOM id so the measured height is cached under
          // the same key the window math looks up (see renderPermissionItem).
          render: () => renderPermissionItem(permissionEvent, toolResults, agentId, resolution, permKey),
        });
      } else if (item.kind === "chip") {
        const chipEvent = item.event;
        if (!isHiddenUserMessage(chipEvent)) {
          push({
            key: chipEvent.event_id,
            estimate: ESTIMATED_USER_HEIGHT_PX,
            start_offset: offsetOf(chipEvent.event_id),
            render: () => renderUserMessage(chipEvent) as m.Vnode,
          });
        }
      }
    }
    for (const event of section.trailing_reply) {
      push({
        key: event.event_id,
        estimate: ESTIMATED_ASSISTANT_HEIGHT_PX,
        start_offset: offsetOf(event.event_id),
        render: () => renderAssistantMessage(event, toolResults, agentId),
      });
    }
  }
  return rows;
}

/**
 * Close every row's range so the loaded window is covered end to end.
 *
 * Each row claims from where it starts to where the next one does, and the last
 * runs to the end of the window. This is what keeps events that render nothing
 * -- a hidden user message, the auth-error prefix, a tk lifecycle marker -- from
 * leaving holes: a hole would later be filled with the learned per-event
 * estimate, reserving space for content that has none. Absorbing them into the
 * row above instead makes the reserved height for a measured range exactly the
 * height that range renders at.
 *
 * Rows are emitted in transcript order, so the ranges come out sorted and
 * non-overlapping, which is what the geometry index assumes.
 */
function makeRangesContiguous(rows: RowDescriptor[], windowStart: number, windowEnd: number): void {
  if (rows.length === 0) {
    return;
  }
  // Leading hidden events belong to the first row rather than to a gap above it.
  rows[0].start_offset = windowStart;
  for (let i = 0; i < rows.length - 1; i++) {
    // Guard against a non-monotonic offset (an event id the window no longer
    // holds resolves to the window start): never let a row run backwards.
    rows[i + 1].start_offset = Math.max(rows[i].start_offset, rows[i + 1].start_offset);
    rows[i].end_offset = rows[i + 1].start_offset;
  }
  const last = rows[rows.length - 1];
  last.end_offset = Math.max(last.start_offset + 1, windowEnd);
}

/**
 * The full events -> virtualized rows pipeline shared by both conversation
 * views: hide the pre-login auth-error prefix, walk the transcript into turn
 * sections, then flatten into top-level rows. The structure and decoration --
 * which steps exist, their order, grouping, titles, summaries -- come purely
 * from the transcript walk.
 */
export function buildConversationRows(
  agentId: string,
  events: TranscriptEvent[],
  agentIsIdle: boolean,
  firstOffset = 0,
): RowDescriptor[] {
  const toolResults = buildToolResultsWithSkillExpansions(events);
  const hiddenEventIds = computeAuthErrorHiddenEventIds(events);
  const visibleEvents = hiddenEventIds.size > 0 ? events.filter((e) => !hiddenEventIds.has(e.event_id)) : events;
  const sections = buildSections(visibleEvents, toolResults, agentIsIdle);
  // Built from the unfiltered events, so a hidden event still resolves to its
  // real transcript position and the rows around it land on the right offsets.
  const offsetByEventId = new Map<string, number>();
  for (let i = 0; i < events.length; i++) {
    offsetByEventId.set(events[i].event_id, firstOffset + i);
  }
  const offsetOf = (eventId: string | undefined): number =>
    eventId === undefined ? firstOffset : (offsetByEventId.get(eventId) ?? firstOffset);
  const rows = buildRows(agentId, sections, toolResults, offsetOf);
  makeRangesContiguous(rows, firstOffset, firstOffset + events.length);
  return rows;
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
