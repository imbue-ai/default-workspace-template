/**
 * A compaction of the agent's context, as chips in the transcript's chip rows.
 *
 * A compaction shows as two chips beside the agent's tool calls: one for its
 * start, named as the activity strip names it while it runs ("Compacting while
 * idle…"), and one for its finish ("Compacted"). Both panels open on why it ran
 * and where to change that. The start chip's then shows the `/compact` the idle
 * sweep sent, when the transcript has it, and the finish chip's the summary the
 * agent carries on from. While a compaction runs, its start chip stands alone
 * where the pair will land (see `buildSections`).
 */

import m from "mithril";
import { renderMarkdown } from "../markdown";
import type { CompactionCause, UserMessageEvent } from "../models/Response";
import { compactingLabel } from "./ActivityIndicator";
import { classifyUserMessage } from "./message-classification";
import { closeChip, type StatusChip, type StatusChipSection } from "./ToolChipGroup";

/** The id of the compaction running now, which has no event of its own yet. */
export const RUNNING_COMPACTION_ID = "compaction-running";

/** A compaction where the transcript walk placed it: one that landed (its "Context was
 *  compacted" event), or the one running now. Shaped to sit in an assistant run beside
 *  the events whose tool calls it shares a chip row with. */
export interface CompactionPart {
  type: "compaction";
  /** The event's id, or `RUNNING_COMPACTION_ID`. */
  event_id: string;
  event: UserMessageEvent | null;
  cause: CompactionCause | null;
  /** The `/compact` the idle sweep sent for it, shown on its start chip in place of a bubble. */
  command: UserMessageEvent | null;
  /** Whether the one-time Auto-compact notice goes under this compaction's chip row. */
  isNoticeAnchor: boolean;
}

export function landedCompaction(
  event: UserMessageEvent,
  isNoticeAnchor: boolean,
  command: UserMessageEvent | null = null,
): CompactionPart {
  return {
    type: "compaction",
    event_id: event.event_id,
    event,
    cause: event.compaction_cause ?? null,
    command,
    isNoticeAnchor,
  };
}

export function runningCompaction(
  cause: CompactionCause | null,
  command: UserMessageEvent | null = null,
): CompactionPart {
  return { type: "compaction", event_id: RUNNING_COMPACTION_ID, event: null, cause, command, isNoticeAnchor: false };
}

/** Why the context was compacted and where to change it, as markdown, for who started the compaction. */
function compactionExplanation(cause: CompactionCause | null): string {
  switch (cause) {
    case "idle":
      return "Compacted while idle to keep replies fast and cheap. Change this under Auto-compact in the model menu.";
    case "manual":
      return "Compacted because you asked (`/compact`).";
    case "native":
      return "Your agent triggered compaction. You can ask it about its current setting, or tell it to change it.";
    default:
      return "Compacted to keep replies fast and cheap. Idle compaction is under Auto-compact in the model menu.";
  }
}

/** The summary the compacted event carries, if any. */
function eventSummary(event: UserMessageEvent): string {
  const { label, body } = classifyUserMessage(event);
  return label !== null && body !== label ? body : "";
}

/** The panel's first part. Composed here from the cause: it is for the user only, so it
 *  never enters the transcript the agent reads. */
function renderExplanation(cause: CompactionCause | null): m.Vnode {
  return m(
    "div",
    { class: "compaction-explanation markdown-content" },
    // The text classes sit on an inner element: .markdown-content is unlayered CSS, so on the
    // same element it would beat these utilities with its body size and primary colour.
    m(
      "div",
      { class: "py-0.5 text-(length:--font-size-helper) leading-normal text-secondary" },
      m.trust(renderMarkdown(compactionExplanation(cause))),
    ),
  );
}

/** A chip's panel: the explanation, then the chip's own output when it has one. */
function panelSections(cause: CompactionCause | null, marker: string, output: string): StatusChipSection[] {
  const sections: StatusChipSection[] = [{ kind: "content", render: () => renderExplanation(cause) }];
  if (output) sections.push({ kind: "output", marker, text: output });
  return sections;
}

/** The chips a compaction shows as: its start, and its finish once it has landed. */
export function compactionChips(part: CompactionPart): StatusChip[] {
  const started: StatusChip = {
    kind: "status",
    id: `compaction-started:${part.event_id}`,
    label: compactingLabel(part.cause, false),
    icon: "package-open",
    chipClass: "compaction-chip compaction-chip--started",
    detailClass: "compaction-detail",
    sections: panelSections(part.cause, "compaction-command", part.command?.content.trim() ?? ""),
  };
  if (part.event === null) return [started];
  return [
    started,
    {
      kind: "status",
      id: `compaction-finished:${part.event_id}`,
      label: "Compacted",
      icon: "package",
      chipClass: "compaction-chip compaction-chip--finished",
      detailClass: "compaction-detail",
      sections: panelSections(part.cause, "compaction-summary", eventSummary(part.event)),
    },
  ];
}

/** Close the running compaction's chip. Every running compaction reuses its id, and the
 *  expansion store is never swept, so one left open would open the next compaction's too. */
export function closeRunningCompactionChips(): void {
  for (const chip of compactionChips(runningCompaction(null))) closeChip(chip);
}
