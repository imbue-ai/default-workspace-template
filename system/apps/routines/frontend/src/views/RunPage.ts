/** A run: what the finished event says, the links it left, and its log as is. */

import m from "mithril";

import type { RunDetail } from "../models/api";
import { chatUrlOf } from "../models/chatLink";
import { logPathOf, routinePathOf } from "../models/routing";

export interface RunPageAttrs {
  detail: RunDetail;
  routineTitle: string;
}

/** Whether the log's view reaches its end (within a pixel of rounding). */
function isScrolledToEnd(log: Element): boolean {
  return log.scrollTop + log.clientHeight >= log.scrollHeight - 1;
}

export const RunPage: m.ClosureComponent<RunPageAttrs> = () => {
  // Whether the reader was at the log's end before the last re-read patched it: a run that is going grows
  // the log under them, and the end stays in view unless they scrolled up to read something.
  let wasLogAtEnd = true;
  return {
    view({ attrs: { detail, routineTitle } }) {
      const { run } = detail;
      return m("div", { class: "mx-auto flex max-w-[1040px] flex-col gap-5 px-4 py-6 sm:px-7" }, [
        m("nav", { class: "type-helper" }, [
          m("a", { href: "/", class: "text-secondary hover:underline" }, "All routines"),
          m("span", { class: "mx-2 text-faint" }, "/"),
          m("a", { href: routinePathOf(run.routine_id), class: "text-secondary hover:underline" }, routineTitle),
        ]),
        m("header", { class: "flex flex-col gap-1" }, [
          m("h1", { class: "type-heading-lg text-primary" }, `${run.outcome_text}, ${run.when_text}`),
          m("p", { class: "type-body text-secondary" }, [
            run.trigger === "MANUAL" ? "Started by you" : "Started on its schedule",
            run.duration_text === "" ? "" : `, took ${run.duration_text}`,
            run.exit_code === null ? "" : `, exit code ${run.exit_code}`,
          ]),
          run.summary === "" ? null : m("p", { class: "type-body text-primary", "data-summary": "" }, run.summary),
          run.warnings.includes("RESTARTED_PREVIOUS_RUN")
            ? m(
                "p",
                { class: "type-helper text-warning" },
                "The previous run was still going and was cut off so this one could start.",
              )
            : null,
        ]),
        detail.links.length === 0 && run.chat_id === null
          ? null
          : m("ul", { class: "flex flex-wrap gap-3 type-helper" }, [
              run.chat_id === null
                ? null
                : m(
                    "li",
                    m("a", { href: chatUrlOf(run.chat_id), class: "text-accent hover:underline" }, "Open its chat"),
                  ),
              detail.links.map((link) =>
                m("li", m("a", { href: link.url, class: "text-accent hover:underline" }, link.label)),
              ),
            ]),
        m("section", [
          m("div", { class: "flex items-baseline justify-between" }, [
            m("h2", { class: "type-section mb-1 text-faint" }, "Log"),
            m(
              "a",
              { href: logPathOf(run.routine_id, run.run_id), class: "type-helper text-secondary hover:underline" },
              "Whole log",
            ),
          ]),
          detail.is_log_truncated ? m("p", { class: "type-helper text-faint" }, "Only the end is shown here.") : null,
          m(
            "pre",
            {
              class:
                "max-h-[70vh] overflow-auto rounded-lg bg-inverse p-3 font-mono text-(length:--font-size-helper) whitespace-pre-wrap text-on-accent",
              "data-log": "",
              oncreate: ({ dom }) => {
                dom.scrollTop = dom.scrollHeight;
              },
              onbeforeupdate: (_vnode, old) => {
                wasLogAtEnd = isScrolledToEnd(old.dom);
              },
              onupdate: ({ dom }) => {
                if (wasLogAtEnd) dom.scrollTop = dom.scrollHeight;
              },
            },
            detail.log === "" ? "Nothing was printed." : detail.log,
          ),
        ]),
      ]);
    },
  };
};
