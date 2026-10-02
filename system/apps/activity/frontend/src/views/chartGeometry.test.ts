import { describe, expect, it } from "vitest";

import { HOUR_END_SECONDS, period } from "../testing/records";
import {
  CHART_BOX,
  bandPoints,
  chartMaxKib,
  linePoints,
  nearestPeriod,
  periodLabel,
  timeTicks,
  unbrokenRuns,
  xFor,
  yFor,
} from "./chartGeometry";
import type { ChartScale } from "./chartGeometry";

const SCALE: ChartScale = { startSeconds: 0, endSeconds: 600, maxKib: 1000 };
const PLOT_WIDTH = CHART_BOX.width - CHART_BOX.left - CHART_BOX.right;
const PLOT_HEIGHT = CHART_BOX.height - CHART_BOX.top - CHART_BOX.bottom;

describe("the chart's scales", () => {
  it("place the span's ends at the plot's edges and clamp anything outside it", () => {
    expect(xFor(CHART_BOX, SCALE, 0)).toBe(CHART_BOX.left);
    expect(xFor(CHART_BOX, SCALE, 600)).toBe(CHART_BOX.left + PLOT_WIDTH);
    expect(xFor(CHART_BOX, SCALE, 300)).toBe(CHART_BOX.left + PLOT_WIDTH / 2);
    expect(xFor(CHART_BOX, SCALE, -50)).toBe(CHART_BOX.left);
    expect(xFor(CHART_BOX, SCALE, 9_999)).toBe(CHART_BOX.left + PLOT_WIDTH);
  });

  it("put zero at the bottom and the top of the scale at the top", () => {
    expect(yFor(CHART_BOX, SCALE, 0)).toBe(CHART_BOX.top + PLOT_HEIGHT);
    expect(yFor(CHART_BOX, SCALE, 1000)).toBe(CHART_BOX.top);
    expect(yFor(CHART_BOX, SCALE, 5000)).toBe(CHART_BOX.top);
    expect(yFor(CHART_BOX, { ...SCALE, maxKib: 0 }, 10)).toBe(CHART_BOX.top + PLOT_HEIGHT);
  });

  it("top out at the limit when it is known, else just above the highest reading", () => {
    const periods = [period(0, 100, 50, 400)];
    expect(chartMaxKib(periods, 8000)).toBe(8000);
    expect(chartMaxKib(periods, null)).toBeCloseTo(440);
    expect(chartMaxKib([], null)).toBe(1);
  });
});

describe("runs of readings", () => {
  it("break wherever recording missed a period, so no line crosses a gap", () => {
    const periods = [period(0, 1, 1, 1), period(60, 2, 2, 2), period(300, 3, 3, 3), period(360, 4, 4, 4)];
    expect(unbrokenRuns(periods, 60).map((run) => run.map((entry) => entry.start_epoch_seconds))).toEqual([
      [0, 60],
      [300, 360],
    ]);
    expect(unbrokenRuns([], 60)).toEqual([]);
  });

  it("draw the line through each period's middle at its typical reading, and the band from highest to lowest", () => {
    const run = [period(0, 500, 250, 1000), period(60, 500, 500, 500)];
    const firstX = (CHART_BOX.left + (30 / 600) * PLOT_WIDTH).toFixed(1);
    const secondX = (CHART_BOX.left + (90 / 600) * PLOT_WIDTH).toFixed(1);
    const middleY = (CHART_BOX.top + PLOT_HEIGHT / 2).toFixed(1);
    expect(linePoints(CHART_BOX, SCALE, run, 60)).toBe(`${firstX},${middleY} ${secondX},${middleY}`);
    const band = bandPoints(CHART_BOX, SCALE, run, 60).split(" ");
    expect(band).toEqual([
      `${firstX},${CHART_BOX.top.toFixed(1)}`,
      `${secondX},${middleY}`,
      `${secondX},${middleY}`,
      `${firstX},${(CHART_BOX.top + PLOT_HEIGHT * 0.75).toFixed(1)}`,
    ]);
  });
});

describe("hovering", () => {
  it("finds the period whose middle is nearest the pointer", () => {
    const periods = [period(0, 1, 1, 1), period(300, 2, 2, 2)];
    expect(nearestPeriod(CHART_BOX, SCALE, periods, 60, CHART_BOX.left)?.start_epoch_seconds).toBe(0);
    expect(nearestPeriod(CHART_BOX, SCALE, periods, 60, CHART_BOX.left + PLOT_WIDTH)?.start_epoch_seconds).toBe(300);
    expect(nearestPeriod(CHART_BOX, SCALE, [], 60, 100)).toBeNull();
  });
});

describe("time ticks", () => {
  it("are four labels from the span's start to its end", () => {
    const ticks = timeTicks("HOUR", HOUR_END_SECONDS - 3600, HOUR_END_SECONDS);
    expect(ticks.map((tick) => tick.at)).toEqual([
      HOUR_END_SECONDS - 3600,
      HOUR_END_SECONDS - 2400,
      HOUR_END_SECONDS - 1200,
      HOUR_END_SECONDS,
    ]);
    expect(ticks.every((tick) => tick.label.length > 0)).toBe(true);
  });
});

describe("period labels", () => {
  it("name the time of a period in every range, and its weekday beyond an hour", () => {
    const at = Date.parse("2026-10-01T09:30:00Z") / 1000;
    const hourLabel = periodLabel("HOUR", at);
    const weekLabel = periodLabel("WEEK", at);
    const weekLater = periodLabel("WEEK", at + 30 * 60);
    expect(hourLabel).toMatch(/\d/);
    expect(weekLabel).not.toBe(weekLater);
    expect(weekLabel.length).toBeGreaterThan(hourLabel.length);
  });
});
