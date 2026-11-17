/**
 * The views of the two resources the sampler graphs: the CPU (how busy it is, what is using it, its busy
 * stretches) and memory (how full it is, the memory guard's line, what is using it, what the guard stopped).
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility name a contiguous
 * literal.
 */

import m from "mithril";

import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { noticeClass } from "@imbue/workspace-ui/src/components/notice";

import type { Span, StatsResponse, StopTarget } from "../models/api";
import {
  GUARD_PERCENT,
  MEMORY_WARN_PERCENT,
  ago,
  cpuRows,
  memoryNote,
  pct,
  plural,
  shownRows,
  size,
} from "../models/format";
import type { MonitorModel } from "../models/monitor";
import { Chart } from "./Chart";
import { EventList, StopList } from "./EventList";
import { INKS, ROW_CLASS, bigFigure, hint, meter, section, sectionHeading } from "./parts";
import { UsageTable } from "./UsageTable";

export interface ResourceViewAttrs {
  model: MonitorModel;
  onStop: (target: StopTarget, name: string) => void;
}

function onSpan(model: MonitorModel, kind: "cpu" | "memory"): (span: Span) => void {
  return (span) => void model.chooseSpan(kind, span);
}

export const CpuView: m.Component<ResourceViewAttrs> = {
  view({ attrs: { model, onStop } }) {
    const stats: StatsResponse | null = model.stats;
    const cpu = stats?.cpu ?? null;
    const hasCpu = cpu !== null && cpu.sampled_at !== null;
    const percent = cpu !== null && hasCpu ? Math.round(cpu.usage * 100) : 0;
    return m("div", { class: "view", "data-view": "cpu" }, [
      section({}, [
        bigFigure(
          hasCpu ? `${percent}%` : "--",
          cpu === null ? "" : hasCpu ? `of ${plural(cpu.cores, "core")} busy` : "no reading yet",
        ),
        meter(percent, INKS.cpu.bar, "mt-2.5 mb-1.5 h-2"),
        m("div", { class: ROW_CLASS }, [
          m(
            "span",
            cpu === null
              ? ""
              : cpu.sampled_at !== null
                ? `read ${ago(Date.now() / 1000 - cpu.sampled_at)}`
                : "the sampler has not reported",
          ),
          m("span", cpu === null ? "" : plural(cpu.processes, "process", "processes")),
        ]),
        m(Chart, {
          title: "CPU in use",
          spanLabel: "Span of the CPU graph",
          label: "CPU in use over time",
          samples: model.samples("cpu"),
          sampleKey: "cpu",
          span: model.spans.cpu,
          onSpan: onSpan(model, "cpu"),
          ink: INKS.cpu,
          isLoaded: stats !== null,
          format: (value) => `${Math.round(value * 100)}%`,
        }),
      ]),
      section({}, [
        sectionHeading("What's using the CPU"),
        hint("A chat's share includes whatever it is running: tests, builds, a browser."),
        m(UsageTable, { rows: stats === null ? [] : cpuRows(stats), columns: ["cpu"], onStop }),
      ]),
      section({}, [
        sectionHeading("Busy stretches", "last 24 hours"),
        stats === null
          ? null
          : m(EventList, {
              events: stats.events.filter((event) => event.kind === "busy"),
              emptyText: "No busy stretches in the last 24 hours.",
            }),
      ]),
    ]);
  },
};

function memoryLegend(): m.Vnode {
  return m("div", { class: "mt-2 flex flex-wrap justify-center gap-3.5 type-helper text-body-content-secondary" }, [
    m("span", { class: "inline-flex items-center gap-1.5" }, [
      m("i", { class: `inline-block h-0.5 w-3.5 ${INKS.memory.bar}` }),
      "In use",
    ]),
    m("span", { class: "inline-flex items-center gap-1.5" }, [
      m("i", {
        class:
          "inline-block h-px w-3.5 bg-[repeating-linear-gradient(90deg,currentColor_0_3px,transparent_3px_6px)] text-danger-surface",
      }),
      "Programs stopped above this",
    ]),
  ]);
}

export const MemoryView: m.Component<ResourceViewAttrs> = {
  view({ attrs: { model, onStop } }) {
    const stats = model.stats;
    const memory = stats?.memory ?? null;
    const percent = memory === null ? 0 : pct(memory.used, memory.total);
    const isShort = percent >= MEMORY_WARN_PERCENT;
    const note = stats === null ? null : memoryNote(stats);
    return m("div", { class: "view", "data-view": "memory" }, [
      section({}, [
        bigFigure(memory === null ? "--" : size(memory.used), [
          "of ",
          m("span", memory === null ? "--" : size(memory.total)),
          " used",
        ]),
        meter(
          percent,
          isShort ? "bg-warn-surface" : INKS.memory.bar,
          "guard-meter relative mt-2.5 mb-1.5 h-2",
          m("i", {
            class: "absolute -top-[3px] -bottom-[3px] w-px bg-danger-surface opacity-70",
            style: { left: `${GUARD_PERCENT}%` },
            ...hoverTooltipAttrs(`Above ${GUARD_PERCENT}% the workspace stops programs to free memory`),
          }),
        ),
        m("div", { class: ROW_CLASS }, [
          m("span", memory === null ? "" : `${percent}% used`),
          m("span", memory === null ? "" : `${size(memory.available)} free`),
        ]),
        m(Chart, {
          title: "Memory in use",
          spanLabel: "Span of the memory graph",
          label: "memory in use over time",
          samples: model.samples("memory"),
          sampleKey: "memory",
          span: model.spans.memory,
          onSpan: onSpan(model, "memory"),
          ink: INKS.memory,
          isLoaded: stats !== null,
          isGuarded: true,
          format: (value) => `${size(value * (memory?.total ?? 0))} (${Math.round(value * 100)}%)`,
          legend: memoryLegend(),
        }),
        note === null
          ? null
          : m(
              "div",
              { class: noticeClass("warn", "memory-note mt-3") },
              m("p", [m("strong", note.head), note.rest === "" ? null : ` ${note.rest}`]),
            ),
      ]),
      section({}, [
        sectionHeading("What's using memory"),
        stats !== null && stats.usage_mode !== "exact" ? hint("Memory use is approximate.") : null,
        m(UsageTable, { rows: stats === null ? [] : shownRows(stats.usage), columns: ["mem"], onStop }),
      ]),
      section({}, [
        sectionHeading("Programs stopped to free memory", "last 24 hours"),
        stats === null
          ? null
          : m(StopList, {
              stops: stats.stops,
              openStops: model.openStops,
              onToggle: (at) => model.toggleStopDetails(at),
            }),
      ]),
    ]);
  },
};
