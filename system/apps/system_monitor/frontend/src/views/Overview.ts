/**
 * The overview: a tile per resource (each a way to its view), what is using the workspace, and the events of the
 * last 24 hours.
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility name a contiguous
 * literal.
 */

import m from "mithril";

import { icon } from "@imbue/workspace-ui/src/components/icons";
import { TILE_PART } from "@imbue/workspace-ui/src/themes/parts";

import type { StatsResponse, StopTarget } from "../models/api";
import { pct, plural, shownRows, size } from "../models/format";
import { miniSpark } from "./Chart";
import { EventList } from "./EventList";
import { INKS, hint, meter, section, sectionHeading } from "./parts";
import { UsageTable } from "./UsageTable";

export interface OverviewAttrs {
  stats: StatsResponse | null;
  onStop: (target: StopTarget, name: string) => void;
}

const TILE_CLASS =
  "tile block rounded-xl border border-default bg-body-surface-raised px-3 pt-3 pb-2.5 text-body-content no-underline " +
  "transition-colors duration-(--dur-base) hover:border-default-hover hover:bg-body-surface-raised-hover " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus " +
  "max-[480px]:grid max-[480px]:grid-cols-[1fr_120px] max-[480px]:items-center max-[480px]:gap-x-3 max-[480px]:py-2.5";

function tile(href: string, label: string, value: string, unit: string, graph: m.Vnode): m.Vnode {
  return m("a", { ...TILE_PART, class: TILE_CLASS, href, "aria-label": `Open the ${label} view` }, [
    m(
      "div",
      { class: "mb-1 flex items-center justify-between type-helper text-body-content-secondary max-[480px]:mb-0.5" },
      [
        m("span", label),
        m(
          "span",
          { class: "text-body-content-tertiary", "aria-hidden": "true" },
          m.trust(icon("chevron-right", { size: 14 })),
        ),
      ],
    ),
    m("div", { class: "type-heading whitespace-nowrap tabular-nums" }, [
      m("span", { "data-tile-value": label }, value),
      m("small", { class: "ml-1 type-helper text-body-content-secondary" }, unit),
    ]),
    graph,
  ]);
}

export const Overview: m.Component<OverviewAttrs> = {
  view({ attrs: { stats, onStop } }) {
    const history = stats?.history ?? [];
    const cpu = stats?.cpu ?? null;
    const hasCpu = cpu !== null && cpu.sampled_at !== null;
    const memoryPercent = stats === null ? null : pct(stats.memory.used, stats.memory.total);
    const diskPercent = stats === null ? null : pct(stats.disk.used, stats.disk.total);
    return m("div", { class: "view", "data-view": "overview" }, [
      section({}, [
        m("div", { class: "grid grid-cols-3 gap-2.5 max-[480px]:grid-cols-1 max-[480px]:gap-2" }, [
          tile(
            "/cpu",
            "CPU",
            cpu !== null && hasCpu ? `${Math.round(cpu.usage * 100)}%` : "--",
            cpu === null ? "" : hasCpu ? `of ${plural(cpu.cores, "core")}` : "no reading",
            miniSpark(history, "cpu", INKS.cpu, "CPU over the last 10 minutes"),
          ),
          tile(
            "/memory",
            "Memory",
            memoryPercent === null ? "--" : `${memoryPercent}%`,
            stats === null ? "" : `of ${size(stats.memory.total)}`,
            miniSpark(history, "memory", INKS.memory, "memory over the last 10 minutes"),
          ),
          tile(
            "/disk",
            "Disk",
            diskPercent === null ? "--" : `${diskPercent}%`,
            stats === null ? "" : `of ${size(stats.disk.total)}`,
            meter(
              diskPercent ?? 0,
              INKS.disk.bar,
              "mt-6 h-1 max-[480px]:col-start-2 max-[480px]:row-span-2 max-[480px]:row-start-1 max-[480px]:mt-0 max-[480px]:self-center max-[480px]:h-1.5",
            ),
          ),
        ]),
      ]),
      section({}, [
        sectionHeading("What's using the workspace"),
        stats !== null && stats.usage_mode !== "exact" ? hint("Memory use is approximate.") : null,
        m(UsageTable, {
          rows: stats === null ? [] : shownRows(stats.usage),
          columns: ["cpu", "mem"],
          hasHeader: true,
          onStop,
        }),
      ]),
      section({}, [
        sectionHeading("Recent events", "last 24 hours"),
        stats === null
          ? null
          : m(EventList, { events: stats.events, emptyText: "Nothing in the last 24 hours.", hasDetailsLinks: true }),
      ]),
    ]);
  },
};
