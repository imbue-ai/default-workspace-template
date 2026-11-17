/**
 * The disk view: how full the disk is, what is on it (an on-demand walk, since measuring takes a moment), and the
 * state of the backups.
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility name a contiguous
 * literal.
 */

import m from "mithril";

import { Button } from "@imbue/workspace-ui/src/components/Button";

import type { DiskResponse, DiskScan } from "../models/api";
import { backupLines, backupSchedule, clock, pct, size } from "../models/format";
import {
  DISK_SHADES,
  DISK_SHADE_FALLBACK,
  HINT_CLASS,
  INKS,
  ROW_CLASS,
  TAG_CLASS,
  bigFigure,
  dot,
  meter,
  section,
  sectionHeading,
} from "./parts";

export interface DiskViewAttrs {
  disk: DiskResponse | null;
  isScanRequested: boolean;
  onScan: () => void;
}

const TABLE_CLASS = "w-full border-collapse type-body-dense tabular-nums";
const CELL_CLASS = "border-b border-subtle py-1.5 text-left align-top";
const NUM_CLASS = "w-[72px] text-right whitespace-nowrap max-[480px]:w-14";
const ITEM_CLASS = "pl-4 text-body-content-secondary";

const SCAN_BUTTON_LABELS: Partial<Record<DiskScan["state"], string>> = {
  scanning: "Measuring…",
  done: "Measure again",
  failed: "Try again",
};

function scanNote(scan: DiskScan | null): string {
  if (scan?.state === "done" && scan.scanned_at !== null) return `As of ${clock(new Date(scan.scanned_at))}.`;
  if (scan?.state === "scanning") return "Measuring what is on the disk…";
  if (scan?.state === "failed" && scan.scanned_at !== null) {
    return `Could not measure what is on the disk (tried at ${clock(new Date(scan.scanned_at))}).`;
  }
  return "Measuring what is on the disk takes a moment, so it runs only when you ask.";
}

function hasResult(scan: DiskScan): boolean {
  return scan.state === "done" || (scan.state === "scanning" && scan.rows.length > 0);
}

function tagged(text: string, tag: string | undefined): m.Children {
  return [text, tag ? [" ", m("span", { class: `${TAG_CLASS} max-[480px]:hidden` }, tag)] : null];
}

function scanTable(scan: DiskScan): m.Vnode {
  return m(
    "table",
    { class: `disk-table ${TABLE_CLASS}` },
    m(
      "tbody",
      scan.rows.flatMap((row) => [
        m("tr", { key: `row:${row.id}`, "data-group": row.id }, [
          m("td", { class: `${CELL_CLASS} font-medium text-body-content` }, [
            m("i", {
              class: `mr-2 inline-block size-2 rounded-[2px] ${DISK_SHADES[row.id] ?? DISK_SHADE_FALLBACK}`,
              "aria-hidden": "true",
            }),
            tagged(row.label, row.hint),
          ]),
          m("td", { class: `${CELL_CLASS} ${NUM_CLASS} font-medium text-body-content` }, size(row.bytes)),
        ]),
        ...row.items.map((item, index) =>
          m("tr", { key: `item:${row.id}:${index}:${item.name}`, "data-item": item.name }, [
            m("td", { class: `${CELL_CLASS} ${ITEM_CLASS}` }, tagged(item.name, item.hint)),
            m("td", { class: `${CELL_CLASS} ${NUM_CLASS} text-body-content-secondary` }, size(item.bytes)),
          ]),
        ),
      ]),
    ),
  );
}

function backupSummary(disk: DiskResponse): m.Children {
  const backups = disk.backups;
  const [tone, head, sub] = backupLines(backups);
  return [
    dot(tone, "self-center"),
    m("span", { class: "type-body text-body-content" }, [m("b", { class: "font-bold" }, head), ` ${sub}`]),
    backups.state === "not_configured"
      ? null
      : m("p", { class: "col-start-2 type-body-dense text-body-content-secondary" }, backupSchedule(backups)),
    m(
      "a",
      {
        class: "col-start-2 mt-1.5 type-body-dense text-accent-surface no-underline hover:underline",
        href: `file://${backups.readme_path}`,
      },
      "Learn more about backups ›",
    ),
  ];
}

export const DiskView: m.Component<DiskViewAttrs> = {
  view({ attrs: { disk, isScanRequested, onScan } }) {
    const figures = disk?.disk ?? null;
    const percent = figures === null ? 0 : pct(figures.used, figures.total);
    const scan = disk?.scan ?? null;
    return m("div", { class: "view", "data-view": "disk" }, [
      section({}, [
        bigFigure(figures === null ? "--" : size(figures.used), [
          "of ",
          m("span", figures === null ? "--" : size(figures.total)),
          " used",
        ]),
        meter(percent, INKS.disk.bar, "mt-2.5 mb-1.5 h-2"),
        m("div", { class: ROW_CLASS }, [
          m("span", figures === null ? "" : `${percent}% used`),
          m("span", figures === null ? "" : `${size(figures.free)} free`),
        ]),
        m(
          "p",
          { class: `mt-1 ${HINT_CLASS}` },
          "The disk of the machine this workspace runs on. Free is what this workspace can still store there: on a " +
            "cloud machine, its share of the disk can run out before the disk does.",
        ),
      ]),
      section({}, [
        sectionHeading("What's using the disk"),
        m("div", { class: "mb-3 flex items-center justify-between gap-3" }, [
          m("p", { class: "scan-note type-body-dense text-body-content-secondary" }, scanNote(scan)),
          m(
            Button,
            {
              sm: true,
              extra: "scan-button",
              disabled: isScanRequested || scan?.state === "scanning",
              onclick: onScan,
            },
            (scan === null ? undefined : SCAN_BUTTON_LABELS[scan.state]) ?? "See what's using the disk",
          ),
        ]),
        // A walk's rows stay up while the next one runs, until it ends done or failed.
        scan !== null && hasResult(scan) ? scanTable(scan) : null,
        scan !== null && hasResult(scan) && scan.missing.length
          ? m("p", { class: `scan-missing mt-2 ${HINT_CLASS}` }, `Could not measure: ${scan.missing.join(", ")}.`)
          : null,
      ]),
      section({}, [
        sectionHeading("Backups"),
        disk === null
          ? null
          : m(
              "div",
              { class: "backup grid grid-cols-[auto_1fr] items-baseline gap-x-2.5 gap-y-0.5" },
              backupSummary(disk),
            ),
      ]),
    ]);
  },
};
