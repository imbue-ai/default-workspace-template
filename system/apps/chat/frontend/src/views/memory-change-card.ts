/**
 * The memory-change card: what the agent changed in its memory during one reply, at the
 * point in the conversation where it did so, with the changed lines and an Undo button.
 *
 * Claude's change-log hook (system/scripts/agent_memory_change_log.py) records each reply's
 * memory changes as one change and tells the agent about it; the backend draws that note
 * as a "Memory changed" notice carrying the change's id (`memory_change_id`). This card is
 * that notice opened up: it reads the change from `GET /api/memory-changes/:id` and undoes
 * it through `POST /api/memory-changes/:id/undo` (the agent_memory library does both).
 *
 * The seam: `MemoryChangeCard` is the live component (it fetches, holds the expanded and
 * busy state, and handles Undo), and `renderMemoryChangeCard` is the pure renderer the
 * tests drive directly.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { Button, buttonClass, buttonPartAttrs } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
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
  const a = before === null ? [] : before.split("\n");
  const b = after === null ? [] : after.split("\n");
  const table: number[][] = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const lines: [DiffLineKind, string][] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      lines.push(["same", a[i++]]);
      j++;
    } else if (table[i + 1][j] >= table[i][j + 1]) lines.push(["removed", a[i++]]);
    else lines.push(["added", b[j++]]);
  }
  while (i < a.length) lines.push(["removed", a[i++]]);
  while (j < b.length) lines.push(["added", b[j++]]);
  const isNearChange = (k: number) => [k - 1, k, k + 1].some((x) => lines[x] !== undefined && lines[x][0] !== "same");
  const shown: [DiffLineKind, string][] = [];
  let folded = 0;
  const flush = () => {
    if (folded > 0) shown.push(["fold", `${folded} unchanged line${folded === 1 ? "" : "s"}`]);
    folded = 0;
  };
  lines.forEach((line, k) => {
    if (line[0] === "same" && !isNearChange(k)) {
      folded++;
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
  /** The summary the notice carried, shown before the change has loaded. */
  summary: string;
  change: MemoryChange | null;
  isExpanded: boolean;
  isBusy: boolean;
  error: string;
}

export interface MemoryChangeCardHandlers {
  onToggle(): void;
  onUndo(): void;
}

/** The card itself, from its state: the notice line with its two actions, then (expanded) each file's changed lines. */
export function renderMemoryChangeCard(state: MemoryChangeCardState, handlers: MemoryChangeCardHandlers): m.Vnode {
  const change = state.change;
  const isUndone = change !== null && change.undone_at !== null;
  const summary = change?.summary ?? state.summary;
  return m("div", { class: "memory-change-card flex flex-col gap-1.5 text-(length:--font-size-helper)" }, [
    m("div", { class: "flex flex-wrap items-center gap-x-2 gap-y-1" }, [
      m(
        "span",
        { class: `mt-px shrink-0 ${isUndone ? "text-faint" : "text-accent"}`, ...railNodeAttrs("point") },
        m.trust(icon(isUndone ? "refresh" : "check", { size: 13, strokeWidth: 2.5 })),
      ),
      m("span", { class: "min-w-0 text-secondary" }, [
        m("span", { class: "font-medium text-primary" }, isUndone ? "Memory change undone:" : "Memory changed:"),
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
            isUndone
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
    state.error ? m("span", { class: "memory-change-error text-danger" }, state.error) : null,
    state.isExpanded && change !== null
      ? m("div", { class: "flex flex-col gap-2 pl-5" }, change.files.map(renderFile))
      : null,
  ]);
}

async function requestJson(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
): Promise<{ status: number; data: unknown }> {
  const response = await fetch(apiUrl(path), {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    // A non-JSON body leaves the status to speak.
  }
  return { status: response.status, data };
}

function detailOf(data: unknown, fallback: string): string {
  const detail = (data as { detail?: unknown } | null)?.detail;
  return typeof detail === "string" ? detail : fallback;
}

/** The live card for one change: loads it, and undoes it (asking first when a file was changed again since). */
export function MemoryChangeCard(): m.Component<{ changeId: string; summary: string }> {
  const state: MemoryChangeCardState = { summary: "", change: null, isExpanded: false, isBusy: false, error: "" };
  let changeId = "";

  async function load(): Promise<void> {
    try {
      const { status, data } = await requestJson("GET", `/api/memory-changes/${encodeURIComponent(changeId)}`);
      if (status === 200) state.change = data as MemoryChange;
      else state.error = detailOf(data, "Couldn't load this memory change.");
    } catch {
      state.error = "The chat app could not be reached.";
    }
    m.redraw();
  }

  async function undo(force: boolean): Promise<void> {
    state.isBusy = true;
    state.error = "";
    try {
      const { status, data } = await requestJson("POST", `/api/memory-changes/${encodeURIComponent(changeId)}/undo`, {
        force,
      });
      if (status === 200) {
        state.change = data as MemoryChange;
      } else if (status === 409 && window.confirm(`${detailOf(data, "")} Undo anyway?`)) {
        await undo(true);
        return;
      } else if (status !== 409) {
        state.error = detailOf(data, `Undo failed (HTTP ${status}).`);
      }
    } catch {
      state.error = "The chat app could not be reached.";
    }
    state.isBusy = false;
    m.redraw();
  }

  return {
    oninit(vnode) {
      changeId = vnode.attrs.changeId;
      state.summary = vnode.attrs.summary;
      void load();
    },
    view() {
      return renderMemoryChangeCard(state, {
        onToggle: () => {
          state.isExpanded = !state.isExpanded;
        },
        onUndo: () => void undo(false),
      });
    },
  };
}
