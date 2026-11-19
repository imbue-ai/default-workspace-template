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
 * The seam: `MemoryChangeCard` is the live component (it fetches, holds the expanded and
 * busy state, and handles Undo), and `renderMemoryChangeCard` is the pure renderer the
 * tests drive directly.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { Button, buttonClass, buttonPartAttrs } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { HttpError, requestJson } from "@imbue/workspace-ui/src/models/http";
import { railNodeAttrs } from "./agent-rail";

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
    m("span", { class: "text-faint" }, file.label),
    m(
      "div",
      {
        class:
          "memory-change-diff m-0 flex flex-col rounded-md bg-surface-secondary px-2 py-1.5 whitespace-pre-wrap break-words",
      },
      lineDiff(file.before_text, file.after_text).map(([kind, text]) =>
        m(
          "div",
          { class: `memory-change-line diff-${kind} rounded-sm px-1 ${DIFF_LINE_CLASS[kind]}` },
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

/** The ask after a refused Undo: why it was refused, then "Undo anyway" or "Keep it". */
function renderConfirm(detail: string, state: MemoryChangeCardState, handlers: MemoryChangeCardHandlers): m.Vnode {
  return m("div", { class: "memory-change-confirm flex flex-wrap items-center gap-x-2 gap-y-1 pl-5" }, [
    m("span", { class: "text-secondary" }, detail),
    m("span", { class: "flex gap-1" }, [
      m(
        Button,
        {
          variant: "secondary",
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
          variant: "ghost",
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

/** The card itself, from its state: the notice line with its actions, then (expanded) each file's changed lines. */
export function renderMemoryChangeCard(state: MemoryChangeCardState, handlers: MemoryChangeCardHandlers): m.Vnode {
  const change = state.change;
  const isUndone = change !== null && change.undone_at !== null;
  const isConfirming = state.confirmDetail !== null;
  const summary = change?.summary ?? state.summary;
  return m("div", { class: "memory-change-card flex flex-col gap-1.5 text-(length:--font-size-helper)" }, [
    m("div", { class: "flex flex-wrap items-center gap-x-2 gap-y-1" }, [
      m(
        "span",
        { class: `mt-px shrink-0 ${isUndone ? "text-faint" : "text-accent"}`, ...railNodeAttrs("point") },
        m.trust(icon(isUndone ? "refresh" : "check", { size: 13, strokeWidth: 2.5 })),
      ),
      m("span", { class: "min-w-0 text-secondary" }, [
        m("span", { class: "font-medium text-primary" }, isUndone ? "Memory change undone:" : `${state.label}:`),
        ` ${summary}`,
      ]),
      change === null
        ? null
        : m("span", { class: "flex gap-1" }, [
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
            isUndone || isConfirming
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
          ]),
    ]),
    state.confirmDetail !== null && !isUndone ? renderConfirm(state.confirmDetail, state, handlers) : null,
    state.error ? m("span", { class: "memory-change-error text-danger" }, state.error) : null,
    state.isExpanded && change !== null
      ? m("div", { class: "flex flex-col gap-2 pl-5" }, change.files.map(renderFile))
      : null,
  ]);
}

const UNREACHABLE_DETAIL = "The chat app could not be reached.";

function changeUrl(changeId: string, suffix: string): string {
  return apiUrl(`/api/memory-changes/${encodeURIComponent(changeId)}${suffix}`);
}

/** The live card for one change: loads it, and undoes it (asking in the card first when a file changed again since). */
export function MemoryChangeCard(): m.Component<{ changeId: string; label: string; summary: string }> {
  const state: MemoryChangeCardState = {
    label: "",
    summary: "",
    change: null,
    isExpanded: false,
    isBusy: false,
    error: "",
    confirmDetail: null,
  };
  let changeId = "";

  async function load(): Promise<void> {
    try {
      state.change = await requestJson<MemoryChange>("GET", changeUrl(changeId, ""));
    } catch (error) {
      state.error = error instanceof HttpError ? error.message : UNREACHABLE_DETAIL;
    }
    m.redraw();
  }

  async function undo(isForced: boolean): Promise<void> {
    state.isBusy = true;
    state.error = "";
    m.redraw();
    try {
      state.change = await requestJson<MemoryChange>("POST", changeUrl(changeId, "/undo"), { is_forced: isForced });
      state.confirmDetail = null;
    } catch (error) {
      if (error instanceof HttpError && error.status === 409) state.confirmDetail = error.message;
      else state.error = error instanceof HttpError ? error.message : UNREACHABLE_DETAIL;
    }
    state.isBusy = false;
    m.redraw();
  }

  return {
    oninit(vnode) {
      changeId = vnode.attrs.changeId;
      state.label = vnode.attrs.label;
      state.summary = vnode.attrs.summary;
      void load();
    },
    view() {
      return renderMemoryChangeCard(state, {
        onToggle: () => {
          state.isExpanded = !state.isExpanded;
        },
        onUndo: () => void undo(false),
        onUndoAnyway: () => void undo(true),
        onKeep: () => {
          state.confirmDetail = null;
        },
      });
    },
  };
}
