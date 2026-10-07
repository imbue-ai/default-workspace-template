/**
 * A compaction of the agent's context, as chips in the transcript's chip rows.
 *
 * A compaction shows as two chips beside the agent's tool calls: one for its
 * start, named as the activity strip names it while it runs ("Compacting while
 * idle…"), and one for its finish ("Compacted while idle"). Both open the same
 * panel: why it ran and where to change that, then the summary the agent carries
 * on from. While a compaction runs, its start chip stands alone where the pair
 * will land (see `buildSections`).
 */

import m from "mithril";
import { renderMarkdown } from "../markdown";
import type { CompactionCause, UserMessageEvent } from "../models/Response";
import { compactingLabel } from "./ActivityIndicator";
import { classifyUserMessage } from "./message-classification";
import type { StatusChip, StatusChipSection } from "./ToolChipGroup";

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
  /** Whether the one-time Auto-compact notice goes under this compaction's chip row. */
  isNoticeAnchor: boolean;
}

export function landedCompaction(event: UserMessageEvent, isNoticeAnchor: boolean): CompactionPart {
  return {
    type: "compaction",
    event_id: event.event_id,
    event,
    cause: event.compaction_cause ?? null,
    isNoticeAnchor,
  };
}

export function runningCompaction(cause: CompactionCause | null): CompactionPart {
  return { type: "compaction", event_id: RUNNING_COMPACTION_ID, event: null, cause, isNoticeAnchor: false };
}

/** A finished compaction's label for who started it, or null to keep the event's own. */
function compactedLabel(cause: CompactionCause | null): string | null {
  switch (cause) {
    case "manual":
      return "Compacted as requested";
    case "idle":
      return "Compacted while idle";
    case "native":
      return "Compacted to free up context";
    default:
      return null;
  }
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

/** The event's own label ("Context was compacted") and the summary it carries, if any. */
function eventLabelAndSummary(event: UserMessageEvent): { label: string; summary: string } {
  const cls = classifyUserMessage(event);
  const label = cls.label ?? (cls.body || "Context was compacted");
  return { label, summary: cls.body && cls.body !== label ? cls.body : "" };
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

/** The panel both chips open: the explanation, then the summary once there is one. */
function panelSections(cause: CompactionCause | null, summary: string): StatusChipSection[] {
  const sections: StatusChipSection[] = [{ kind: "content", render: () => renderExplanation(cause) }];
  if (summary) sections.push({ kind: "output", marker: "compaction-summary", text: summary });
  return sections;
}

/** The chips a compaction shows as: its start, and its finish once it has landed. */
export function compactionChips(part: CompactionPart): StatusChip[] {
  const { label: eventLabel, summary } =
    part.event === null ? { label: "", summary: "" } : eventLabelAndSummary(part.event);
  const sections = panelSections(part.cause, summary);
  const started: StatusChip = {
    kind: "status",
    id: `compaction-started:${part.event_id}`,
    label: compactingLabel(part.cause, false),
    icon: "package-open",
    chipClass: "compaction-chip compaction-chip--started",
    detailClass: "compaction-detail",
    sections,
  };
  if (part.event === null) return [started];
  return [
    started,
    {
      kind: "status",
      id: `compaction-finished:${part.event_id}`,
      label: compactedLabel(part.cause) ?? eventLabel,
      icon: "package",
      chipClass: "compaction-chip compaction-chip--finished",
      detailClass: "compaction-detail",
      sections,
    },
  ];
}
