import { describe, expect, it } from "vitest";
import { themeMetricsRecord } from "../testing/records";
import { clampedWidgetSize, widgetPositionOf, widgetRects } from "./widgets";

const METRICS = themeMetricsRecord();
const BACKDROP = { width: 1200, height: 800 };

describe("widget geometry", () => {
  it("clamps a reported size to the theme's minimum and maximum", () => {
    expect(clampedWidgetSize({ width: 180, height: 64 }, METRICS)).toEqual({ width: 180, height: 64 });
    expect(clampedWidgetSize({ width: 2000, height: 2000 }, METRICS)).toEqual({ width: 320, height: 240 });
    expect(clampedWidgetSize({ width: 0, height: 5 }, METRICS)).toEqual({ width: 24, height: 24 });
  });

  it("stacks unmoved widgets down from the top right, a rim round each page, below a title bar", () => {
    const rects = widgetRects(
      [
        { app: "gauge", position: null, size: { width: 180, height: 64 } },
        { app: "clock", position: { x: 0.25, y: 0.5 }, size: { width: 120, height: 30 } },
        { app: "meter", position: null, size: { width: 100, height: 40 } },
      ],
      BACKDROP,
      METRICS,
    );

    expect(rects.get("gauge")).toEqual({ x: 1200 - 16 - 192, y: 36 + 16, width: 192, height: 76 });
    // A moved widget takes no place in the stack.
    expect(rects.get("meter")).toEqual({ x: 1200 - 16 - 112, y: 36 + 16 + 76 + 8, width: 112, height: 52 });
  });

  it("anchors a moved widget by its corner nearest the edges, and keeps it inside the backdrop", () => {
    const rects = widgetRects(
      [
        { app: "upper-left", position: { x: 0.1, y: 0.1 }, size: { width: 120, height: 30 } },
        { app: "lower-right", position: { x: 0.9, y: 0.9 }, size: { width: 120, height: 30 } },
        { app: "dial", position: { x: 0, y: 1 }, size: { width: 60, height: 10 } },
      ],
      BACKDROP,
      METRICS,
    );

    expect(rects.get("upper-left")).toEqual({ x: 120, y: 80, width: 132, height: 42 });
    expect(rects.get("lower-right")).toEqual({ x: 1080 - 132, y: 720 - 42, width: 132, height: 42 });
    // Below the minimum, a page's report is raised to it.
    expect(rects.get("dial")).toEqual({ x: 0, y: 800 - 36, width: 72, height: 36 });
    // On a backdrop too narrow for it where it was put, it is shifted back in.
    const narrow = widgetRects(
      [{ app: "wide", position: { x: 0.45, y: 0.1 }, size: { width: 300, height: 100 } }],
      { width: 400, height: 300 },
      METRICS,
    );
    expect(narrow.get("wide")).toEqual({ x: 400 - 312, y: 30, width: 312, height: 112 });
  });

  it("stores a let-go box by its corner nearest the edges, which reads back to the same box", () => {
    const upperRight = { x: 900, y: 60, width: 132, height: 42 };
    expect(widgetPositionOf(upperRight, BACKDROP)).toEqual({ x: 1032 / 1200, y: 60 / 800 });
    const lowerLeft = { x: 40, y: 600, width: 132, height: 42 };
    const position = widgetPositionOf(lowerLeft, BACKDROP);
    expect(position).toEqual({ x: 40 / 1200, y: 642 / 800 });
    expect(
      widgetRects([{ app: "a", position, size: { width: 120, height: 30 } }], BACKDROP, METRICS).get("a"),
    ).toEqual(lowerLeft);
  });
});
