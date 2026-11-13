/**
 * The breakdown of what is using the workspace: one row per kind of thing (chats, code checkers, apps, the
 * workspace's own programs, temporary files, ...) with what it breaks down into under it, each with its share of
 * the CPU, its memory, or both, and a Stop button on what can be stopped from here. The button shows while the
 * pointer is over its row (always where there is no pointer to hover with), and asks before anything stops.
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility name a contiguous
 * literal.
 */

import m from "mithril";

import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";

import type { StopTarget, UsageItem, UsageRow } from "../models/api";
import { STOP_WORDS, sharePct, size, usageTag } from "../models/format";
import { TAG_CLASS } from "./parts";

export type UsageColumn = "cpu" | "mem";

export interface UsageTableAttrs {
  rows: readonly UsageRow[];
  /** Which figures each row shows, in order. */
  columns: readonly UsageColumn[];
  /** Head the figures' columns (the overview, which shows both). */
  hasHeader?: boolean;
  onStop: (target: StopTarget, name: string) => void;
}

export const SCRATCH_EXPLANATION =
  "Temporary files are created by programs while they work, such as downloads in progress or screenshots from a " +
  "test run. They are kept in memory, so they count toward memory use. They are cleared when the workspace restarts.";

const TABLE_CLASS = "w-full border-collapse type-body-dense tabular-nums";
const CELL_CLASS = "border-b border-subtle py-1.5 text-left align-top";
const NUM_CLASS = "w-[72px] text-right whitespace-nowrap max-[480px]:w-14";
const ITEM_CLASS = "pl-4 text-body-content-secondary";
const ACT_CLASS = "w-7 pl-2 text-right whitespace-nowrap";
const STOP_BUTTON_EXTRA =
  "stop-button opacity-0 group-hover:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100 max-[480px]:opacity-100";
const INFO_CLASS =
  "ml-1 inline-flex cursor-help rounded-sm align-[-2px] text-body-content-tertiary hover:text-body-content-secondary " +
  "focus-visible:text-body-content-secondary focus-visible:outline-2 focus-visible:outline-focus";

function figureCells(figures: UsageRow | UsageItem, columns: readonly UsageColumn[], isItem: boolean): m.Vnode[] {
  return columns.map((column) => {
    const cellClass = `${CELL_CLASS} ${NUM_CLASS} ${isItem ? "text-body-content-secondary" : "font-medium text-body-content"}`;
    if (column === "mem") return m("td", { class: cellClass, "data-mem": "" }, size(figures.bytes));
    const share = sharePct(figures.cpu);
    return m(
      "td",
      { class: cellClass, "data-cpu": "" },
      share === null ? m("span", { class: "text-body-content-tertiary" }, "–") : share,
    );
  });
}

function stopCell(item: UsageItem | null, onStop: UsageTableAttrs["onStop"]): m.Vnode {
  const target = item?.stop ?? null;
  if (item === null || target === null) return m("td", { class: `${CELL_CLASS} ${ACT_CLASS}` });
  const { verb } = STOP_WORDS[target.kind];
  return m(
    "td",
    { class: `${CELL_CLASS} ${ACT_CLASS}` },
    m(
      Button,
      {
        variant: "ghost-destructive",
        icon: true,
        xs: true,
        extra: STOP_BUTTON_EXTRA,
        "aria-label": `${verb} ${item.name}`,
        "data-stop-kind": target.kind,
        "data-stop-id": target.id,
        ...hoverTooltipAttrs(verb),
        onclick: () => onStop(target, item.name),
      },
      m.trust(icon("close", { size: 14 })),
    ),
  );
}

function rowHeading(row: UsageRow): m.Children {
  const tag = usageTag(row);
  return [
    row.label,
    row.id === "scratch"
      ? m(
          "button",
          {
            type: "button",
            class: INFO_CLASS,
            "aria-label": "What are temporary files?",
            ...hoverTooltipAttrs(SCRATCH_EXPLANATION, "below", { maxWidth: 260 }),
          },
          m.trust(icon("info", { size: 13 })),
        )
      : null,
    tag ? [" ", m("span", { class: `${TAG_CLASS} max-[480px]:hidden` }, tag)] : null,
  ];
}

export const UsageTable: m.Component<UsageTableAttrs> = {
  view({ attrs: { rows, columns, hasHeader = false, onStop } }) {
    return m("table", { class: TABLE_CLASS }, [
      hasHeader
        ? m(
            "thead",
            m("tr", [
              m("th", { class: `${CELL_CLASS} type-section text-body-content-tertiary` }),
              ...columns.map((column) =>
                m(
                  "th",
                  { class: `${CELL_CLASS} ${NUM_CLASS} type-section text-body-content-tertiary` },
                  column === "cpu" ? "CPU" : "Memory",
                ),
              ),
              m("th", { class: `${CELL_CLASS} ${ACT_CLASS}` }),
            ]),
          )
        : null,
      m(
        "tbody",
        rows.flatMap((row) => [
          m("tr", { key: `row:${row.id}`, class: "group", "data-group": row.id }, [
            m("td", { class: `${CELL_CLASS} font-medium text-body-content` }, rowHeading(row)),
            ...figureCells(row, columns, false),
            stopCell(null, onStop),
          ]),
          ...row.items.map((item, index) =>
            m("tr", { key: `item:${row.id}:${index}:${item.name}`, class: "group", "data-item": item.name }, [
              m("td", { class: `${CELL_CLASS} ${ITEM_CLASS}` }, item.name),
              ...figureCells(item, columns, true),
              stopCell(item, onStop),
            ]),
          ),
        ]),
      ),
    ]);
  },
};
