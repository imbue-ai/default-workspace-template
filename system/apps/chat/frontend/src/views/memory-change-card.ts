/**
 * The memory-change card: what the agent changed in its memory during one reply, at the
 * point in the conversation where it did so, with the changed lines and an Undo button.
 *
 * Claude's change-log hook (system/scripts/agent_memory_change_log.py) records each reply's
 * memory changes as one change and tells the agent about it; the backend draws that note
 * as a notice (labelled "Memory changed") carrying the change's id (`memory_change_id`). This
 * card is that notice opened up: it reads the change from `GET /api/memory-changes/:id` and
 * undoes it through `POST /api/memory-changes/:id/undo` (the agent_memory library does both);
 * a 409 (a file changed again since) asks in the card whether to undo anyway.
 *
 * The seam: `MemoryChangeCard` is the live component (it reads and undoes the change through
 * `memoryChangeCache`), and `renderMemoryChangeCard` is the pure renderer the tests drive
 * directly. The change's state lives in that page-wide cache and the card's expansion in
 * expansion-state.ts, never in the component: the transcript's virtualized window slides rows
 * out and back in, and a card remounted that way keeps what it loaded, what was undone, and
 * whether it was open.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { Button, buttonClass, buttonPartAttrs } from "@imbue/workspace-ui/src/components/Button";
import { HttpError, requestJson } from "@imbue/workspace-ui/src/models/http";
import { isBlockExpanded, setBlockExpanded } from "./expansion-state";
import { renderNotice } from "./user-message-display";

export interface ChangedMemoryFile {
  name: string;
  before: string | null;
  after: string | null;
  /** What the file is, in words ("New note: Likes tea", "Your index ..."). */
  label: string;
  /** The text as a person reads it: a note's summary and text, without its bookkeeping. */
  before_text: string | null;
  after_text: string | null;
  note_id: string;
  details_url: string | null;
}

export interface MemoryChange {
  id: string;
  time: string;
  summary: string;
  chat_id: string;
  tool_use_ids: string[];
  files: ChangedMemoryFile[];
  undone_at: string | null;
  /** Where the Memory app shows the change's note in full. */
  details_url: string | null;
}

export type DiffLineKind = "same" | "removed" | "added" | "fold";

/** A line-by-line comparison of two texts. Unchanged lines more than one away from a
 *  change fold into a count, so a one-line edit to a long index shows as one line. */
export function lineDiff(before: string | null, after: string | null): [DiffLineKind, string][] {
  const beforeLines = before === null ? [] : before.split("\n");
  const afterLines = after === null ? [] : after.split("\n");
  // commonLength[i][j]: the longest common subsequence of beforeLines[i:] and afterLines[j:].
  const commonLength: number[][] = Array.from({ length: beforeLines.length + 1 }, () =>
    new Array<number>(afterLines.length + 1).fill(0),
  );
  for (let i = beforeLines.length - 1; i >= 0; i--) {
    for (let j = afterLines.length - 1; j >= 0; j--) {
      commonLength[i][j] =
        beforeLines[i] === afterLines[j]
          ? commonLength[i + 1][j + 1] + 1
          : Math.max(commonLength[i + 1][j], commonLength[i][j + 1]);
    }
  }
  const lines: [DiffLineKind, string][] = [];
  let i = 0;
  let j = 0;
  while (i < beforeLines.length && j < afterLines.length) {
    if (beforeLines[i] === afterLines[j]) {
      lines.push(["same", beforeLines[i++]]);
      j++;
    } else if (commonLength[i + 1][j] >= commonLength[i][j + 1]) lines.push(["removed", beforeLines[i++]]);
    else lines.push(["added", afterLines[j++]]);
  }
  while (i < beforeLines.length) lines.push(["removed", beforeLines[i++]]);
  while (j < afterLines.length) lines.push(["added", afterLines[j++]]);
  const isNearChange = (position: number) =>
    [position - 1, position, position + 1].some(
      (neighbour) => lines[neighbour] !== undefined && lines[neighbour][0] !== "same",
    );
  const shown: [DiffLineKind, string][] = [];
  let foldedCount = 0;
  const flush = () => {
    if (foldedCount > 0) shown.push(["fold", `${foldedCount} unchanged line${foldedCount === 1 ? "" : "s"}`]);
    foldedCount = 0;
  };
  lines.forEach((line, position) => {
    if (line[0] === "same" && !isNearChange(position)) {
      foldedCount++;
      return;
    }
    flush();
    shown.push(line);
  });
  flush();
  return shown;
}

// Colour carries what changed (no "+"/"-" marks): green is new, red and struck through is gone.
const DIFF_LINE_CLASS: Record<DiffLineKind, string> = {
  same: "text-secondary",
  removed: "bg-danger-surface line-through decoration-danger/60",
  added: "bg-success-surface",
  fold: "text-faint italic",
};

function renderFile(file: ChangedMemoryFile): m.Vnode {
  return m("div", { class: "memory-change-file flex flex-col gap-1" }, [
    m("span", { class: "memory-change-file-label text-faint" }, file.label),
    m(
      "div",
      {
        class:
          "memory-change-diff flex flex-col rounded-md bg-surface-secondary px-2 py-1.5 whitespace-pre-wrap break-words",
      },
      lineDiff(file.before_text, file.after_text).map(([kind, text]) =>
        m(
          "div",
          { class: `memory-change-line memory-change-line--${kind} rounded-sm px-1 ${DIFF_LINE_CLASS[kind]}` },
          text || "\u00a0",
        ),
      ),
    ),
  ]);
}

export interface MemoryChangeCardState {
  /** The notice's label from the backend ("Memory changed"), which opens the card's line. */
  label: string;
  /** The summary the notice carried, shown before the change has loaded. */
  summary: string;
  change: MemoryChange | null;
  isExpanded: boolean;
  isBusy: boolean;
  error: string;
  /** Why the last Undo was refused (a file changed again since), while the card asks whether to undo anyway. */
  confirmDetail: string | null;
}

export interface MemoryChangeCardHandlers {
  onToggle(): void;
  onUndo(): void;
  onUndoAnyway(): void;
  onKeep(): void;
}

/** The ask after a refused Undo: why it was refused, then "Undo anyway" (which also throws away the later
 *  edits, so it reads as destructive) or "Keep it". */
function renderConfirm(detail: string, state: MemoryChangeCardState, handlers: MemoryChangeCardHandlers): m.Vnode {
  return m("div", { class: "memory-change-confirm flex flex-wrap items-center gap-x-2 gap-y-1 pl-5" }, [
    m("span", { class: "memory-change-confirm-detail text-secondary" }, detail),
    m("span", { class: "memory-change-confirm-actions flex gap-1" }, [
      m(
        Button,
        {
          variant: "destructive",
          sm: true,
          extra: "memory-change-undo-anyway",
          disabled: state.isBusy,
          onclick: () => handlers.onUndoAnyway(),
        },
        "Undo anyway",
      ),
      m(
        Button,
        {
          sm: true,
          extra: "memory-change-keep",
          disabled: state.isBusy,
          onclick: () => handlers.onKeep(),
        },
        "Keep it",
      ),
    ]),
  ]);
}

/** The card's actions beside the notice's line: show or hide the lines, open the note, and Undo. */
function renderActions(
  change: MemoryChange,
  state: MemoryChangeCardState,
  handlers: MemoryChangeCardHandlers,
  isUndone: boolean,
): m.Vnode {
  return m("span", { class: "memory-change-actions flex gap-1" }, [
    m(
      Button,
      { variant: "ghost", sm: true, extra: "memory-change-toggle", onclick: () => handlers.onToggle() },
      state.isExpanded ? "Hide changes" : "Show changes",
    ),
    change.details_url
      ? m(
          "a",
          {
            class: buttonClass("ghost", { sm: true, extra: "memory-change-details no-underline" }),
            ...buttonPartAttrs("ghost", false),
            href: change.details_url,
            target: "_blank",
            rel: "noopener noreferrer",
          },
          "View details",
        )
      : null,
    isUndone || state.confirmDetail !== null
      ? null
      : m(
          Button,
          {
            variant: "ghost",
            sm: true,
            extra: "memory-change-undo",
            disabled: state.isBusy,
            onclick: () => handlers.onUndo(),
          },
          "Undo",
        ),
  ]);
}

/** The card itself, from its state: the notice line with its actions, then (expanded) each file's changed lines. */
export function renderMemoryChangeCard(state: MemoryChangeCardState, handlers: MemoryChangeCardHandlers): m.Vnode {
  const change = state.change;
  const isUndone = change !== null && change.undone_at !== null;
  return m(
    "div",
    { class: `memory-change-card ${isUndone ? "memory-change-card--undone" : ""} flex flex-col gap-1.5` },
    [
      renderNotice(isUndone ? "Memory change undone" : state.label, change?.summary ?? state.summary, {
        iconName: isUndone ? "refresh" : "check",
        isIconFaint: isUndone,
        extra: "memory-change-header flex-wrap",
        actions: change === null ? null : renderActions(change, state, handlers, isUndone),
      }),
      state.confirmDetail !== null && !isUndone ? renderConfirm(state.confirmDetail, state, handlers) : null,
      state.error ? m("span", { class: "memory-change-error type-helper text-danger" }, state.error) : null,
      state.isExpanded && change !== null
        ? m("div", { class: "memory-change-files type-helper flex flex-col gap-2 pl-5" }, change.files.map(renderFile))
        : null,
    ],
  );
}

const UNREACHABLE_DETAIL = "The chat app could not be reached.";

function changeUrl(changeId: string, suffix: string): string {
  return apiUrl(`/api/memory-changes/${encodeURIComponent(changeId)}${suffix}`);
}

/** One change as far as this page knows it: what was loaded, an Undo in flight, and what went wrong. */
interface MemoryChangeEntry {
  change: MemoryChange | null;
  isBusy: boolean;
  error: string;
  confirmDetail: string | null;
}

/**
 * The live cards' state, one entry per change, kept for the page's life so a remounted card keeps it.
 * Every change bumps a version that the memoized row around a card compares (see StableUserMessage), the
 * way expansion-state.ts's version does for expansions.
 */
export class MemoryChangeCache {
  private readonly entries = new Map<string, MemoryChangeEntry>();
  private version = 0;

  getVersion(): number {
    return this.version;
  }

  /** The change's entry; the first ask for a change starts loading it. */
  entry(changeId: string): MemoryChangeEntry {
    const known = this.entries.get(changeId);
    if (known !== undefined) return known;
    const fresh: MemoryChangeEntry = { change: null, isBusy: false, error: "", confirmDetail: null };
    this.entries.set(changeId, fresh);
    void this.load(changeId);
    return fresh;
  }

  /** Undo the change; a 409 (a file changed again since) leaves the reason for the card to ask about. */
  async undo(changeId: string, isForced: boolean): Promise<void> {
    this.update(changeId, { isBusy: true, error: "" });
    try {
      const change = await requestJson<MemoryChange>("POST", changeUrl(changeId, "/undo"), { is_forced: isForced });
      this.update(changeId, { change, confirmDetail: null, isBusy: false });
    } catch (error) {
      if (error instanceof HttpError && error.status === 409) {
        this.update(changeId, { confirmDetail: error.message, isBusy: false });
      } else {
        this.update(changeId, { error: describeFailure(error), isBusy: false });
      }
    }
  }

  /** Drop the question a refused Undo raised, keeping the change as it is. */
  keep(changeId: string): void {
    this.update(changeId, { confirmDetail: null });
  }

  /** Forget every change; for tests, which reuse ids across cases. */
  clearForTesting(): void {
    this.entries.clear();
    this.version += 1;
  }

  private async load(changeId: string): Promise<void> {
    try {
      this.update(changeId, { change: await requestJson<MemoryChange>("GET", changeUrl(changeId, "")) });
    } catch (error) {
      this.update(changeId, { error: describeFailure(error) });
    }
  }

  private update(changeId: string, patch: Partial<MemoryChangeEntry>): void {
    const known = this.entries.get(changeId) ?? { change: null, isBusy: false, error: "", confirmDetail: null };
    this.entries.set(changeId, { ...known, ...patch });
    this.version += 1;
    m.redraw();
  }
}

function describeFailure(error: unknown): string {
  return error instanceof HttpError ? error.message : UNREACHABLE_DETAIL;
}

/** The live cards' cache: one per page. */
export const memoryChangeCache = new MemoryChangeCache();

/** The expansion key of a change's card (expansion-state.ts). */
export function memoryChangeExpansionKey(changeId: string): string {
  return `memory-change:${changeId}`;
}

/** The live card for one change: loads it, and undoes it (asking in the card first when a file changed again since). */
export function MemoryChangeCard(): m.Component<{ changeId: string; label: string; summary: string }> {
  return {
    view(vnode) {
      const { changeId, label, summary } = vnode.attrs;
      const entry = memoryChangeCache.entry(changeId);
      const expansionKey = memoryChangeExpansionKey(changeId);
      const isExpanded = isBlockExpanded(expansionKey);
      return renderMemoryChangeCard(
        { label, summary, isExpanded, ...entry },
        {
          onToggle: () => setBlockExpanded(expansionKey, !isExpanded),
          onUndo: () => void memoryChangeCache.undo(changeId, false),
          onUndoAnyway: () => void memoryChangeCache.undo(changeId, true),
          onKeep: () => memoryChangeCache.keep(changeId),
        },
      );
    },
  };
}
