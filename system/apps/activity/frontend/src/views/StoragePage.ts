/**
 * The storage tab: what is taking up space, in categories a user recognizes (their files, chats and agents, app
 * data, installed tools, download caches, logs), each opening to its folders; the largest folders; and how it was
 * measured. It shows no limit, and says why: the workspace cannot read its disk quota.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import type { StorageState, StorageSummary } from "../models/storage";
import { formatKib, formatShare } from "./format";
import { DETAILS_CLASS, SECTION_HEADING_CLASS, disclosure } from "./styles";

export interface StoragePageAttrs {
  readonly state: StorageState;
  readonly onMeasure: () => void;
}

export function StoragePage(): m.Component<StoragePageAttrs> {
  const openDetails = new Set<string>();

  function disclose(id: string, closedLabel: string, openLabel: string): m.Vnode {
    const isOpen = openDetails.has(id);
    return disclosure(isOpen, closedLabel, openLabel, () => (isOpen ? openDetails.delete(id) : openDetails.add(id)));
  }

  function summaryView(summary: StorageSummary, isMeasuring: boolean, attrs: StoragePageAttrs): m.Vnode {
    const totalKib = Math.max(1, summary.total_kib);
    return m("div", { class: "flex flex-col gap-6" }, [
      m("div", { class: "flex flex-col gap-2" }, [
        m("h2", { class: "m-0 type-heading-lg text-primary text-balance" }, "Here's what's taking up space."),
        m("div", { class: "flex flex-wrap items-center gap-3" }, [
          m(
            "p",
            { class: "m-0 type-body text-secondary" },
            `${formatKib(summary.total_kib)} across the folders below, measured at ${new Date(summary.measured_at).toLocaleTimeString()}.`,
          ),
          m(
            Button,
            { variant: "secondary", sm: true, disabled: isMeasuring, onclick: attrs.onMeasure },
            isMeasuring ? "Measuring…" : "Measure again",
          ),
        ]),
      ]),
      m("section", { class: "flex flex-col" }, [
        m("div", { class: "flex items-baseline justify-between border-b border-default pb-1.5" }, [
          m("h3", { class: `m-0 ${SECTION_HEADING_CLASS}` }, "By kind"),
          m("span", { class: "type-helper tabular-nums text-secondary" }, formatKib(summary.total_kib)),
        ]),
        summary.categories.map((category) =>
          m(
            "div",
            { key: category.category_id, class: "flex flex-col gap-2 border-b border-subtle py-3 last:border-b-0" },
            [
              m("div", { class: "flex items-center gap-4" }, [
                m("div", { class: "flex min-w-0 flex-1 flex-col gap-0.5" }, [
                  m("span", { class: "type-body font-medium text-primary" }, category.name),
                  m("span", { class: "type-helper text-secondary" }, category.description),
                  disclose(`category:${category.category_id}`, "Folders", "Hide folders"),
                ]),
                m("div", { class: "flex w-36 shrink-0 flex-col items-end gap-1" }, [
                  m("span", { class: "type-body tabular-nums text-primary" }, [
                    formatKib(category.size_kib),
                    m("span", { class: "text-secondary" }, ` · ${formatShare(category.size_kib, totalKib)}`),
                  ]),
                  m(
                    "div",
                    { class: "h-1 w-full overflow-hidden rounded-sm bg-surface-secondary", "aria-hidden": "true" },
                    [
                      m("div", {
                        class: "h-full rounded-sm bg-accent",
                        style: { width: `${(100 * category.size_kib) / totalKib}%` },
                      }),
                    ],
                  ),
                ]),
              ]),
              openDetails.has(`category:${category.category_id}`)
                ? m(
                    "div",
                    { class: DETAILS_CLASS },
                    category.folders.length === 0
                      ? m("div", "None of these folders exist yet.")
                      : category.folders.map((folder) =>
                          m("div", { key: folder.path, class: "flex justify-between gap-4" }, [
                            m("span", { class: "break-all" }, folder.path),
                            m("span", { class: "shrink-0 tabular-nums" }, formatKib(folder.size_kib)),
                          ]),
                        ),
                  )
                : null,
            ],
          ),
        ),
      ]),
      m("section", { class: "flex flex-col gap-2" }, [
        m("h3", { class: `m-0 border-b border-default pb-1.5 ${SECTION_HEADING_CLASS}` }, "Largest folders"),
        summary.largest.map((folder) =>
          m("div", { key: folder.path, class: "flex items-baseline justify-between gap-4" }, [
            m("div", { class: "flex min-w-0 flex-col" }, [
              m("span", { class: "break-all font-mono type-helper text-primary" }, folder.path),
              m("span", { class: "type-helper text-secondary" }, folder.category_name),
            ]),
            m("span", { class: "shrink-0 type-body tabular-nums text-primary" }, formatKib(folder.size_kib)),
          ]),
        ),
      ]),
      disclose("storage-how", "How this is measured", "Hide how this is measured"),
      openDetails.has("storage-how")
        ? m("div", { class: DETAILS_CLASS }, [
            m("div", `command    ${summary.command} (took ${summary.measure_seconds} s)`),
            m("div", "limit      not shown: inside the workspace, df reports the host's whole disk, and a cloud"),
            m("div", "           workspace's quota is set outside it and not published inside"),
            m("div", "refresh    only when this tab opens or you press Measure again; du reads every file"),
            ...summary.notes.map((note) => m("div", `note       ${note}`)),
          ])
        : null,
    ]);
  }

  return {
    view: ({ attrs }) => {
      const { state } = attrs;
      switch (state.kind) {
        case "idle":
          return m("p", { class: "m-0 type-body text-secondary" }, "Measuring what's on disk…");
        case "measuring":
          return state.previous === null
            ? m(
                "p",
                { class: "m-0 type-body text-secondary" },
                "Measuring what's on disk… This reads every file, so it can take a few seconds.",
              )
            : summaryView(state.previous, true, attrs);
        case "loaded":
          return summaryView(state.summary, false, attrs);
        case "failed":
          return m("div", { class: "flex flex-col gap-2" }, [
            m("p", { class: "m-0 type-body text-danger-hover" }, `Couldn't measure what's on disk. ${state.message}`),
            m(Button, { variant: "secondary", sm: true, onclick: attrs.onMeasure }, "Try again"),
          ]);
      }
    },
  };
}
