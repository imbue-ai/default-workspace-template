/** A run's whole log, as is, under the same navigation as the run's page. */

import m from "mithril";

import type { RunRecord } from "../models/api";
import { routinePathOf, runPathOf } from "../models/routing";

export interface LogPageAttrs {
  run: RunRecord;
  log: string;
  routineTitle: string;
}

export const LogPage: m.Component<LogPageAttrs> = {
  view({ attrs: { run, log, routineTitle } }) {
    return m("div", { class: "mx-auto flex max-w-[1040px] flex-col gap-5 px-4 py-6 sm:px-7" }, [
      m("nav", { class: "type-helper" }, [
        m("a", { href: "/", class: "text-secondary hover:underline" }, "All routines"),
        m("span", { class: "mx-2 text-faint" }, "/"),
        m("a", { href: routinePathOf(run.routine_id), class: "text-secondary hover:underline" }, routineTitle),
        m("span", { class: "mx-2 text-faint" }, "/"),
        m(
          "a",
          { href: runPathOf(run.routine_id, run.run_id), class: "text-secondary hover:underline" },
          `${run.outcome_text}, ${run.when_text}`,
        ),
      ]),
      m("header", m("h1", { class: "type-heading-lg text-primary" }, "Whole log")),
      m(
        "pre",
        {
          class:
            "rounded-lg bg-inverse p-3 font-mono text-(length:--font-size-helper) whitespace-pre-wrap text-on-accent",
          "data-log": "",
        },
        log === "" ? "Nothing was printed." : log,
      ),
    ]);
  },
};
