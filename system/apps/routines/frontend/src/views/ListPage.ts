/**
 * The list: a summary strip, a field to describe a routine to add, and one row per routine, problems first,
 * grouped by the app a routine belongs to.
 */

import m from "mithril";

import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";

import type { RoutineListing, RoutineRow } from "../models/api";
import { routinePathOf } from "../models/routing";
import { healthPill } from "./healthPill";

export interface ListPageAttrs {
  listing: RoutineListing;
  onStartWithText: (text: string) => void;
}

export const WORKSPACE_GROUP = "Workspace";
export const ADD_PREFIX = "Add a routine: ";

/** The rows under each group heading, in the listing's order: one group per app, headed by its display
 * name, then the workspace's own rows last. Grouped by the app's name, so an app called "Workspace" or two
 * apps sharing a display name stay apart. */
export function groupRows(rows: readonly RoutineRow[], appNames: Record<string, string>): [string, RoutineRow[]][] {
  const byApp = new Map<string | null, RoutineRow[]>();
  for (const row of rows) {
    const group = byApp.get(row.app) ?? [];
    group.push(row);
    byApp.set(row.app, group);
  }
  const ordered: [string, RoutineRow[]][] = [];
  for (const [app, members] of byApp) {
    if (app !== null) ordered.push([appNames[app] ?? app, members]);
  }
  const workspace = byApp.get(null);
  if (workspace !== undefined) ordered.push([WORKSPACE_GROUP, workspace]);
  return ordered;
}

function stat(value: string | number, label: string, isBad: boolean): m.Vnode {
  return m("div", { class: "flex flex-col gap-0.5" }, [
    m("span", { class: `type-heading ${isBad ? "text-danger-surface" : "text-body-content"}` }, String(value)),
    m("span", { class: "type-tag uppercase tracking-wide text-body-content-tertiary" }, label),
  ]);
}

function row(routine: RoutineRow): m.Vnode {
  return m(
    "a",
    {
      href: routinePathOf(routine.routine_id),
      class:
        "routine-row grid gap-x-4 gap-y-1 border-b border-subtle px-1 py-3 no-underline " +
        "hover:bg-fill-hover sm:grid-cols-[minmax(0,1fr)_200px_180px] sm:items-start",
      "data-routine": routine.routine_id,
    },
    [
      m("div", { class: "min-w-0" }, [
        m("div", { class: "type-body font-semibold text-body-content" }, routine.title),
        m("div", { class: "type-helper text-body-content-secondary" }, routine.what),
      ]),
      m("div", { class: "type-helper text-body-content-secondary" }, [
        m("div", routine.schedule_text),
        routine.next_run_text === ""
          ? null
          : m("div", { class: "text-body-content-tertiary" }, `Next: ${routine.next_run_text}`),
      ]),
      m("div", { class: "flex flex-col items-start gap-1 type-helper text-body-content-secondary" }, [
        healthPill(routine),
        routine.health_detail === "" ? null : m("div", { class: "text-body-content-tertiary" }, routine.health_detail),
        m("div", { class: "text-body-content-tertiary" }, routine.last_run_text),
        routine.last_run !== null && routine.last_run.summary !== ""
          ? m("div", { class: "truncate text-body-content-tertiary" }, routine.last_run.summary)
          : null,
        routine.last_run !== null && routine.last_run.warnings.includes("RESTARTED_PREVIOUS_RUN")
          ? m(
              "div",
              { class: "text-warn-surface", "data-warning": "" },
              "The previous run was still going and was cut off.",
            )
          : null,
      ]),
    ],
  );
}

export const ListPage: m.ClosureComponent<ListPageAttrs> = () => {
  // The field's text outlives a redraw (every poll is one), so it lives in the closure, not the view.
  let draft = "";
  return {
    view({ attrs: { listing, onStartWithText } }) {
      const submit = (): void => {
        const text = draft.trim();
        if (text === "") return;
        onStartWithText(ADD_PREFIX + text);
        draft = "";
      };
      const { summary } = listing;
      return m("div", { class: "mx-auto flex max-w-[1040px] flex-col gap-6 px-4 py-6 sm:px-7" }, [
        m("header", { class: "flex flex-col gap-1" }, [
          m("h1", { class: "type-heading-lg text-body-content" }, "Routines"),
          m(
            "p",
            { class: "type-body text-body-content-secondary" },
            "Everything in your workspace that runs on its own.",
          ),
        ]),
        m(
          "form",
          {
            class: "flex gap-2",
            "data-add-form": "",
            onsubmit: (event: Event) => {
              event.preventDefault();
              submit();
            },
          },
          [
            m("input", {
              class: inputClass(),
              type: "text",
              value: draft,
              placeholder: "Describe a routine to add: every weekday at 8, summarize my inbox",
              "aria-label": "Describe a routine to add",
              "data-add-input": "",
              oninput: (event: Event) => {
                draft = (event.target as HTMLInputElement).value;
              },
            }),
            m(Button, { variant: "primary", type: "submit" }, "Add"),
          ],
        ),
        m("div", { class: "flex gap-7 border-b border-default pb-4", "data-summary": "" }, [
          stat(summary.total, "Routines", false),
          stat(summary.needs_attention, "Need attention", summary.needs_attention > 0),
          stat(summary.running, "Running", false),
          stat(summary.next_run_text, "Next run", false),
        ]),
        listing.routines.length === 0
          ? m(
              "p",
              { class: "type-body py-6 text-body-content-tertiary", "data-empty": "" },
              "Nothing runs on its own yet. Describe one above to add it.",
            )
          : groupRows(listing.routines, listing.app_names).map(([heading, rows]) =>
              m("section", { "data-group": heading }, [
                m("h2", { class: "type-section mt-2 mb-1 text-body-content-tertiary" }, heading),
                rows.map(row),
              ]),
            ),
      ]);
    },
  };
};
