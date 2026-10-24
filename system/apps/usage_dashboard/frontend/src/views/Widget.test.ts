// @vitest-environment jsdom

import m from "mithril";
import { describe, expect, it } from "vitest";

import type { LimitReading } from "../models/api";
import { formatClock } from "../models/format";
import { providerCards } from "../models/providers";
import type { WidgetModel } from "../models/widget";
import { DEFAULT_WIDGET_LOOK } from "../models/widgetLook";
import type { WidgetLook } from "../models/widgetLook";
import { totalsRow } from "../testing/rows";
import { Widget } from "./Widget";

const NOW = new Date("2026-10-22T20:00:00Z");

function reading(patch: Partial<LimitReading>): LimitReading {
  return {
    source: "claude",
    window: "five_hour",
    label: "5h",
    used_percentage: 61,
    resets_at: "2026-10-22T22:30:00Z",
    window_seconds: 18_000,
    observed_at: "2026-10-22T19:45:00Z",
    first_used_percentage: null,
    first_observed_at: null,
    ...patch,
  };
}

const WEEKLY = { window: "seven_day", label: "7d", window_seconds: 604_800, resets_at: "2026-10-26T21:00:00Z" };
const SHORT_RESET = formatClock(new Date("2026-10-22T22:30:00Z"));
const WEEKLY_RESET = new Date(WEEKLY.resets_at).toLocaleString(undefined, { weekday: "short", hour: "numeric" });
const API_SPEND = totalsRow({ group: { provider: "anthropic", billing_mode: "API_KEY" }, cost_usd: "9.5" });

function rendered(
  limits: LimitReading[],
  mode: "expanded" | "collapsed" | "tray" | "toolbar",
  look: Partial<WidgetLook> = {},
  totals = [API_SPEND],
): HTMLElement {
  const model = { cards: providerCards(limits, totals, NOW), isLive: true, isCatchingUp: false } as WidgetModel;
  const root = document.createElement("div");
  m.render(root, m(Widget, { model, mode, look: { ...DEFAULT_WIDGET_LOOK, ...look } }));
  return root;
}

function texts(root: Element | null | undefined, selector: string): (string | null)[] {
  return [...(root?.querySelectorAll(selector) ?? [])].map((element) => element.textContent);
}

function tooltips(root: Element | null | undefined, selector: string): (string | null)[] {
  return [...(root?.querySelectorAll(selector) ?? [])].map((element) => element.getAttribute("data-widget-tooltip"));
}

const BOTH = [reading({ used_percentage: 80 }), reading({ ...WEEKLY, used_percentage: 34 })];

describe("the rings version", () => {
  it("collapses to a pair of rings per subscription at their levels, named beneath, with the detail in a tooltip", () => {
    const root = rendered(BOTH, "collapsed");

    const pair = root.querySelector('[data-widget-rings="subscription:claude"]');
    const outer = pair?.querySelector('[data-ring="outer"]');
    const inner = pair?.querySelector('[data-ring="inner"]');
    expect([outer?.getAttribute("r"), outer?.getAttribute("stroke-dasharray")]).toEqual(["18", "34 100"]);
    expect([inner?.getAttribute("r"), inner?.getAttribute("stroke-dasharray")]).toEqual(["13.5", "80 100"]);
    // A comfortable ring keeps its window's hue; a high one takes the level's colour.
    expect([outer?.getAttribute("stroke"), inner?.getAttribute("stroke")]).toEqual([
      "var(--widget-long)",
      "var(--widget-high)",
    ]);
    expect(texts(pair, "[data-ring-figure]")).toEqual(["80%"]);
    expect(texts(pair, "[data-ring-name]")).toEqual(["Claude"]);
    const [summary] = tooltips(pair, "[data-widget-tooltip]");
    expect(summary).toContain(`5-hour limit: 80% used, resets ${SHORT_RESET}`);
    expect(summary).toContain(`Weekly limit: 34% used, resets ${WEEKLY_RESET}`);
    // Nothing waits for the pointer to show itself: no native title, nothing hidden.
    expect(root.querySelector("[title]")).toBeNull();
    expect(root.innerHTML).not.toContain("opacity-0");
  });

  it("draws the 5-hour ring alone with less, and the weekly figure and the 5-hour reset beside it with more", () => {
    const less = rendered(BOTH, "collapsed", { detail: "less" });
    expect(less.querySelector('[data-ring="outer"]')).toBeNull();
    expect(less.querySelector('[data-ring="inner"]')).not.toBeNull();

    const more = rendered(BOTH, "collapsed", { detail: "more" });
    expect(texts(more, "[data-ring-more]")).toEqual(["wk 34%", `5h ${SHORT_RESET}`]);
  });

  it("expands to a row per window with a tooltip each, and shows the resets and the reading's time with more", () => {
    const standard = rendered(BOTH, "expanded");
    const section = standard.querySelector('[data-widget-row="subscription:claude"]');
    expect(texts(section, "[data-widget-percent]")).toEqual(["80%", "34%"]);
    expect(tooltips(section, "[data-widget-window]")).toEqual([
      `5-hour limit: 80% used, resets ${SHORT_RESET}`,
      `Weekly limit: 34% used, resets ${WEEKLY_RESET}`,
    ]);
    expect(section?.querySelector("[data-widget-reset]")).toBeNull();
    expect(standard.textContent).toContain("$9.50");

    const more = rendered(BOTH, "expanded", { detail: "more" });
    expect(texts(more, "[data-widget-reset]")).toEqual([SHORT_RESET, WEEKLY_RESET]);
    expect(more.textContent).toContain("as of");

    const less = rendered(BOTH, "expanded", { detail: "less" });
    expect(texts(less, "[data-widget-percent]")).toEqual(["80%"]);
    expect(less.textContent).not.toContain("$9.50");
  });

  it("names each of several pairs, and leaves a reset window's ring as a bare track", () => {
    const root = rendered(
      [reading({ resets_at: "2026-10-22T19:00:00Z" }), reading({ source: "codex", used_percentage: 18 })],
      "collapsed",
    );

    const pairs = [...root.querySelectorAll("[data-widget-rings]")];
    expect(pairs.map((pair) => pair.querySelector("[data-ring-name]")?.textContent)).toEqual(["Claude", "Codex"]);
    expect(pairs.map((pair) => pair.querySelector("[data-ring-figure]")?.textContent)).toEqual(["—", "18%"]);
    expect(pairs[0].querySelector("[data-ring]")).toBeNull();
    expect(pairs[0].querySelectorAll("circle")).toHaveLength(1);
  });

  it("falls back to the month's spend with no subscription", () => {
    const root = rendered([], "collapsed");
    expect(root.querySelector("[data-widget-rings]")).toBeNull();
    expect(root.textContent).toContain("$9.50");
  });
});

describe("the bars version", () => {
  it("draws the 5-hour bar above the weekly one, each with its window in a tooltip and no label in place", () => {
    const root = rendered(
      [reading({ used_percentage: 61 }), reading({ ...WEEKLY, used_percentage: 92 })],
      "expanded",
      {
        variant: "bars",
      },
    );

    expect(root.querySelector("#usage-widget")?.getAttribute("data-widget-variant")).toBe("bars");
    const row = root.querySelector('[data-widget-row="subscription:claude"]');
    const short = row?.querySelector('[data-widget-bar="short"]') as HTMLElement;
    const long = row?.querySelector('[data-widget-bar="long"]') as HTMLElement;
    expect([short.style.width, short.style.background]).toEqual(["61%", "var(--widget-short)"]);
    expect([long.style.width, long.style.background]).toEqual(["92%", "var(--widget-critical)"]);
    expect(texts(row, "[data-widget-percent]")).toEqual(["61%", "92%"]);
    expect(tooltips(row, "[data-widget-bar-line]")).toEqual([
      `5-hour limit: 61% used, resets ${SHORT_RESET}`,
      `Weekly limit: 92% used, resets ${WEEKLY_RESET}`,
    ]);
    expect(row?.textContent).not.toContain("5-hour");
    expect(row?.querySelector("[data-widget-reset]")).toBeNull();
    expect(root.innerHTML).not.toContain("opacity-0");
  });

  it("shows each bar's reset with more, and the 5-hour bar alone with less", () => {
    const more = rendered(BOTH, "expanded", { variant: "bars", detail: "more" });
    expect(texts(more, "[data-widget-reset]")).toEqual([SHORT_RESET, WEEKLY_RESET]);

    const less = rendered(BOTH, "expanded", { variant: "bars", detail: "less" });
    expect(less.querySelector('[data-widget-bar="long"]')).toBeNull();
    expect(less.textContent).not.toContain("$9.50");
  });

  it("collapses to the name, both bars and the 5-hour figure, the weekly figure too with more", () => {
    const root = rendered(
      [reading({ resets_at: "2026-10-22T19:00:00Z" }), reading({ ...WEEKLY, used_percentage: 34 })],
      "collapsed",
      { variant: "bars" },
    );
    const row = root.querySelector('[data-widget-row="subscription:claude"]');
    expect((row?.querySelector('[data-widget-bar="short"]') as HTMLElement).style.width).toBe("0%");
    expect((row?.querySelector('[data-widget-bar="long"]') as HTMLElement).style.width).toBe("34%");
    expect(texts(row, "[data-widget-percent]")).toEqual(["—"]);

    const more = rendered(BOTH, "collapsed", { variant: "bars", detail: "more" });
    expect(texts(more, "[data-widget-percent]")).toEqual(["80%", "34%"]);
  });

  it("collapses to the month's spend with no subscription", () => {
    const root = rendered([], "collapsed", { variant: "bars" });
    expect(root.querySelector("[data-widget-row]")).toBeNull();
    expect(texts(root, "[data-widget-pill]")).toEqual(["$9.50"]);
  });
});

describe("the widget's size", () => {
  it("names its size on its root, where the stylesheet gives each size its lengths", () => {
    for (const size of ["small", "medium", "large"] as const) {
      const root = rendered(BOTH, "collapsed", { size });
      expect(root.querySelector("#usage-widget")?.getAttribute("data-widget-size")).toBe(size);
    }
  });

  it("lays its root out in one direction per mode: a column on the desktop, a row in the taskbar", () => {
    const directions = (["expanded", "collapsed", "tray", "toolbar"] as const).map((mode) => {
      const classes = rendered(BOTH, mode).querySelector("#usage-widget")?.classList;
      return ["flex-col", "flex-row"].filter((direction) => classes?.contains(direction) === true);
    });
    expect(directions).toEqual([["flex-col"], ["flex-col"], ["flex-row"], ["flex-row"]]);
  });
});

describe("the tray form", () => {
  it("draws a small ring pair per subscription beside its name over the 5-hour figure, the weekly one with more", () => {
    const root = rendered(BOTH, "tray");
    const pair = root.querySelector('[data-widget-rings="subscription:claude"]');
    expect(pair?.querySelector('[data-ring="outer"]')).not.toBeNull();
    // The figure sits beside the ring, not in it.
    expect(pair?.querySelector("[data-ring-figure]")).toBeNull();
    expect(texts(pair, "[data-ring-name]")).toEqual(["Claude"]);
    expect(texts(pair, "[data-widget-percent]")).toEqual(["80%"]);
    expect(tooltips(pair, "[data-widget-percent]")).toEqual([`5-hour limit: 80% used, resets ${SHORT_RESET}`]);
    // No spend in the tray.
    expect(root.textContent).not.toContain("$9.50");

    expect(texts(rendered(BOTH, "tray", { detail: "more" }), "[data-widget-percent]")).toEqual(["80%", "34%"]);
    expect(rendered(BOTH, "tray", { detail: "less" }).querySelector('[data-ring="outer"]')).toBeNull();
  });

  it("draws the bars under the name beside the 5-hour figure in the bars design", () => {
    const root = rendered(BOTH, "tray", { tray: "bars" });
    const row = root.querySelector('[data-widget-row="subscription:claude"]');
    expect(row?.textContent).toContain("Claude");
    expect((row?.querySelector('[data-widget-bar="short"]') as HTMLElement).style.width).toBe("80%");
    expect(row?.querySelector('[data-widget-bar="long"]')).not.toBeNull();
    expect(texts(row, "[data-widget-percent]")).toEqual(["80%"]);
  });
});

describe("the other tray designs", () => {
  it("draws the ring pair alone, with no figure or name, its detail in the tooltip", () => {
    const root = rendered(BOTH, "tray", { tray: "ring" });
    const pair = root.querySelector('[data-widget-rings="subscription:claude"]');
    expect(pair?.querySelector('[data-ring="outer"]')).not.toBeNull();
    expect(pair?.textContent).toBe("");
    expect(tooltips(pair, "[data-widget-tooltip]")[0]).toContain("5-hour limit: 80% used");
    expect(root.querySelector("#usage-widget")?.getAttribute("data-widget-tray")).toBe("ring");
  });

  it("draws the name and the 5-hour figure as text, the weekly one too with more, near-cap figures in colour", () => {
    const pair = rendered(BOTH, "tray", { tray: "numbers" }).querySelector("[data-widget-rings]");
    expect(texts(pair, "[data-ring-name]")).toEqual(["Claude"]);
    expect(texts(pair, "[data-widget-percent]")).toEqual(["80%"]);
    expect((pair?.querySelector("[data-widget-percent]") as HTMLElement).style.color).toBe("var(--widget-high)");
    const more = rendered(BOTH, "tray", { tray: "numbers", detail: "more" });
    expect(texts(more, "[data-widget-percent]")).toEqual(["80%", "34%"]);
  });

  it("stacks the 5-hour figure over the weekly one beside the name, and drops the weekly with less", () => {
    const pair = rendered(BOTH, "tray", { tray: "stacked" }).querySelector("[data-widget-rings]");
    expect(texts(pair, "[data-widget-window] [data-widget-percent]")).toEqual(["80%", "34%"]);
    expect(tooltips(pair, "[data-widget-window] [data-widget-percent]")).toEqual([
      `5-hour limit: 80% used, resets ${SHORT_RESET}`,
      `Weekly limit: 34% used, resets ${WEEKLY_RESET}`,
    ]);
    const less = rendered(BOTH, "tray", { tray: "stacked", detail: "less" });
    expect(texts(less, "[data-widget-window] [data-widget-percent]")).toEqual(["80%"]);
  });
});

describe("the toolbar designs", () => {
  it("draws a chip like a window's entry: the ring pair as its icon, the name and the 5-hour figure as its title", () => {
    const root = rendered(BOTH, "toolbar");
    expect(root.querySelector("#usage-widget")?.getAttribute("data-widget-mode")).toBe("toolbar");
    expect(root.querySelector("#usage-widget")?.getAttribute("data-widget-toolbar")).toBe("chip");
    const pair = root.querySelector('[data-widget-rings="subscription:claude"]');
    expect(pair?.querySelector('[data-ring="outer"]')).not.toBeNull();
    expect(pair?.querySelector("[data-ring-figure]")).toBeNull();
    expect(texts(pair, "[data-ring-name]")).toEqual(["Claude"]);
    expect(texts(pair, "[data-widget-percent]")).toEqual(["80%"]);
    // No spend in the bar.
    expect(root.textContent).not.toContain("$9.50");
    expect(texts(rendered(BOTH, "toolbar", { detail: "more" }), "[data-widget-percent]")).toEqual(["80%", "34%"]);
    expect(rendered(BOTH, "toolbar", { detail: "less" }).querySelector('[data-ring="outer"]')).toBeNull();
  });

  it("draws upright meters filled to each window's share beside the name over the 5-hour figure", () => {
    const pair = rendered(BOTH, "toolbar", { toolbar: "meters" }).querySelector("[data-widget-rings]");
    const short = pair?.querySelector('[data-widget-meter="short"] > span') as HTMLElement;
    expect(short.style.height).toBe("80%");
    expect((pair?.querySelector('[data-widget-meter="long"] > span') as HTMLElement).style.height).toBe("34%");
    expect(tooltips(pair, "[data-widget-meter]")).toEqual([
      `5-hour limit: 80% used, resets ${SHORT_RESET}`,
      `Weekly limit: 34% used, resets ${WEEKLY_RESET}`,
    ]);
    expect(texts(pair, "[data-ring-name]")).toEqual(["Claude"]);
    const less = rendered(BOTH, "toolbar", { toolbar: "meters", detail: "less" });
    expect(less.querySelector('[data-widget-meter="long"]')).toBeNull();
  });

  it("draws the names and 5-hour figures as one line of text", () => {
    const root = rendered(BOTH, "toolbar", { toolbar: "inline" });
    expect(root.querySelector("[data-widget-meter], [data-ring], [data-widget-bar]")).toBeNull();
    expect(texts(root, "[data-ring-name]")).toEqual(["Claude"]);
    expect(texts(root, "[data-widget-percent]")).toEqual(["80%"]);
  });

  it("draws the name and the 5-hour figure over a line per window, the weekly one dropped with less", () => {
    const pair = rendered(BOTH, "toolbar", { toolbar: "line" }).querySelector("[data-widget-rings]");
    expect(texts(pair, "[data-ring-name]")).toEqual(["Claude"]);
    expect((pair?.querySelector('[data-widget-bar="short"]') as HTMLElement).style.width).toBe("80%");
    expect((pair?.querySelector('[data-widget-bar="long"]') as HTMLElement).style.width).toBe("34%");
    const less = rendered(BOTH, "toolbar", { toolbar: "line", detail: "less" });
    expect(less.querySelector('[data-widget-bar="long"]')).toBeNull();
  });
});

describe("the minimal rings", () => {
  it("draws the ring pairs alone with the 5-hour figure in each, at the expanded widget's own width", () => {
    const root = rendered(BOTH, "expanded", { variant: "minimal" });
    const widget = root.querySelector("#usage-widget");
    expect(widget?.getAttribute("data-widget-variant")).toBe("minimal");
    expect(widget?.className).not.toContain("w-(--w-width)");
    const pair = root.querySelector('[data-widget-rings="subscription:claude"]');
    expect(pair?.querySelector('[data-ring="outer"]')).not.toBeNull();
    expect(texts(pair, "[data-ring-figure]")).toEqual(["80%"]);
    // No names, rows, spend or reset text in view; the detail is in the pair's tooltip.
    expect(root.querySelector("[data-ring-name], [data-widget-window], [data-widget-reset]")).toBeNull();
    expect(root.textContent).not.toContain("$9.50");
    expect(tooltips(pair, "[data-widget-tooltip]")[0]).toContain("5-hour limit: 80% used");
  });

  it("drops the weekly ring with less, adds the weekly figure with more, and draws the same when collapsed", () => {
    expect(
      rendered(BOTH, "expanded", { variant: "minimal", detail: "less" }).querySelector('[data-ring="outer"]'),
    ).toBeNull();
    const more = rendered(BOTH, "expanded", { variant: "minimal", detail: "more" });
    expect(texts(more, '[data-ring-more="long"]')).toEqual(["wk 34%"]);
    const collapsed = rendered(BOTH, "collapsed", { variant: "minimal" });
    expect(collapsed.querySelector("#usage-widget")?.getAttribute("data-widget-mode")).toBe("collapsed");
    expect(texts(collapsed, "[data-ring-figure]")).toEqual(["80%"]);
    expect(collapsed.querySelector("[data-ring-name]")).toBeNull();
  });
});
