/**
 * The graphs: a shaded panel with its title and span control, the scale on the left and the line beside it, a
 * readout of the nearest reading where the pointer is, and the overview tiles' small line with no scale.
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility name a contiguous
 * literal.
 */

import m from "mithril";

import { SegmentedControl, type Segment } from "@imbue/workspace-ui/src/components/segmentedControl";

import type { HistorySample, Span } from "../models/api";
import { GUARD_PERCENT, clock } from "../models/format";
import { GAP_MS, SPAN_MS, TEN_MINUTES_MS, nearestSample, spanEnd, sparkPath, type SampleKey } from "../models/spark";

/** The drawing box every graph is laid out in; the SVG stretches it to the space it has. */
const BOX_WIDTH = 300;
const BOX_HEIGHT = 100;
const MINI_BOX_HEIGHT = 40;

const SPAN_SEGMENTS: readonly Segment<Span>[] = [
  { value: "10m", label: "10 min" },
  { value: "1h", label: "1 h" },
  { value: "24h", label: "24 h" },
];

export interface Ink {
  stroke: string;
  fill: string;
}

export interface ChartAttrs {
  title: string;
  /** The span control's name, for a screen reader. */
  spanLabel: string;
  /** The graph's name, for a screen reader. */
  label: string;
  samples: readonly HistorySample[];
  sampleKey: SampleKey;
  span: Span;
  onSpan: (span: Span) => void;
  ink: Ink;
  /** Whether the graph is fetched yet: until then it says nothing about having no readings. */
  isLoaded: boolean;
  /** Draw the memory guard's line. */
  isGuarded?: boolean;
  /** How a reading reads in the readout. */
  format: (value: number) => string;
  legend?: m.Children;
}

interface Readout {
  left: number;
  top: number;
  cursorX: number;
  text: string;
}

const AXIS_STEPS: readonly [string, string][] = [
  ["0%", "100%"],
  ["50%", "50%"],
  ["100%", "0%"],
];

export function Chart(): m.Component<ChartAttrs> {
  let readout: Readout | null = null;
  return {
    view({ attrs }) {
      const { samples, sampleKey, span, ink } = attrs;
      const end = spanEnd(samples);
      const box = { width: BOX_WIDTH, height: BOX_HEIGHT, spanMs: SPAN_MS[span], gapMs: GAP_MS[span], end };
      const onPointerMove = (event: PointerEvent): void => {
        const svg = event.currentTarget as SVGSVGElement;
        const host = svg.parentElement as HTMLElement;
        const svgBox = svg.getBoundingClientRect();
        const hostBox = host.getBoundingClientRect();
        const fraction = Math.min(1, Math.max(0, (event.clientX - svgBox.left) / (svgBox.width || 1)));
        const best = nearestSample(samples, sampleKey, end - (1 - fraction) * SPAN_MS[span]);
        if (best === null) return;
        readout = {
          left: event.clientX - hostBox.left,
          top: svgBox.top - hostBox.top,
          cursorX: fraction * BOX_WIDTH,
          text: `${clock(new Date(best.t))} · ${attrs.format(best[sampleKey] as number)}`,
        };
      };
      return m("div", { class: "chart mt-3 rounded-md bg-body-surface-secondary px-3 pt-3 pb-2" }, [
        m("div", { class: "mb-2 flex items-center justify-between gap-2 type-helper text-body-content-secondary" }, [
          m("span", attrs.title),
          m(SegmentedControl<Span>, {
            label: attrs.spanLabel,
            segments: SPAN_SEGMENTS,
            value: span,
            onSelect: attrs.onSpan,
          }),
        ]),
        m("div", { class: "plot relative grid grid-cols-[auto_1fr] gap-2 py-1" }, [
          m(
            "div",
            {
              class: "relative h-[110px] w-8 type-helper text-body-content-tertiary tabular-nums",
              "aria-hidden": "true",
            },
            AXIS_STEPS.map(([top, label]) =>
              m("span", { class: "absolute right-0 -translate-y-1/2 leading-none", style: { top } }, label),
            ),
          ),
          m(
            "svg",
            {
              class: "spark block h-[110px] w-full overflow-visible",
              viewBox: `0 0 ${BOX_WIDTH} ${BOX_HEIGHT}`,
              preserveAspectRatio: "none",
              "aria-label": attrs.label,
              onpointermove: onPointerMove,
              onpointerleave: () => (readout = null),
            },
            [
              m("path", {
                d: `M0 ${BOX_HEIGHT / 2} H${BOX_WIDTH}`,
                class: "stroke-subtle",
                "stroke-width": "1",
                "vector-effect": "non-scaling-stroke",
              }),
              m("path", {
                d: `M0 ${BOX_HEIGHT} H${BOX_WIDTH}`,
                class: "stroke-default",
                "stroke-width": "1",
                "vector-effect": "non-scaling-stroke",
              }),
              attrs.isGuarded
                ? m("path", {
                    d: `M0 ${BOX_HEIGHT - GUARD_PERCENT} H${BOX_WIDTH}`,
                    class: "guard-line stroke-danger-surface",
                    "stroke-dasharray": "3 3",
                    "stroke-width": "1",
                    opacity: ".6",
                    "vector-effect": "non-scaling-stroke",
                  })
                : null,
              m("path", {
                d: sparkPath(samples, sampleKey, { ...box, isArea: true }),
                class: ink.fill,
                opacity: "0.08",
              }),
              m("path", {
                d: sparkPath(samples, sampleKey, box),
                class: `spark-line fill-none ${ink.stroke}`,
                "stroke-width": "1.6",
                "stroke-linejoin": "round",
                "vector-effect": "non-scaling-stroke",
              }),
              m("line", {
                class: "stroke-body-content-tertiary",
                x1: readout?.cursorX ?? 0,
                x2: readout?.cursorX ?? 0,
                y1: 0,
                y2: BOX_HEIGHT,
                "stroke-width": "1",
                "vector-effect": "non-scaling-stroke",
                opacity: readout === null ? "0" : "1",
              }),
            ],
          ),
          attrs.isLoaded && samples.length === 0
            ? m(
                "div",
                {
                  class:
                    "no-readings absolute inset-0 flex items-center justify-center type-helper text-body-content-tertiary",
                },
                "No readings yet",
              )
            : null,
          readout === null
            ? null
            : m(
                "div",
                {
                  class:
                    "spark-readout pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-[calc(100%+8px)] rounded-md bg-inverse-surface px-2 py-1 type-helper whitespace-nowrap text-inverse-content shadow-overlay",
                  style: { left: `${readout.left}px`, top: `${readout.top}px` },
                },
                readout.text,
              ),
        ]),
        attrs.legend ?? null,
      ]);
    },
  };
}

/** The overview tile's line: the last 10 minutes, no scale, no readout. */
export function miniSpark(samples: readonly HistorySample[], sampleKey: SampleKey, ink: Ink, label: string): m.Vnode {
  const box = {
    width: BOX_WIDTH,
    height: MINI_BOX_HEIGHT,
    spanMs: TEN_MINUTES_MS,
    gapMs: GAP_MS["10m"],
    end: spanEnd(samples),
  };
  return m(
    "svg",
    {
      class:
        "mini-spark mt-2.5 block h-9 w-full max-[480px]:col-start-2 max-[480px]:row-span-2 max-[480px]:row-start-1 max-[480px]:mt-0 max-[480px]:h-8",
      viewBox: `0 0 ${BOX_WIDTH} ${MINI_BOX_HEIGHT}`,
      preserveAspectRatio: "none",
      "aria-label": label,
    },
    [
      m("path", { d: sparkPath(samples, sampleKey, { ...box, isArea: true }), class: ink.fill, opacity: "0.1" }),
      m("path", {
        d: sparkPath(samples, sampleKey, box),
        class: `fill-none ${ink.stroke}`,
        "stroke-width": "1.5",
        "stroke-linejoin": "round",
        "vector-effect": "non-scaling-stroke",
      }),
    ],
  );
}
