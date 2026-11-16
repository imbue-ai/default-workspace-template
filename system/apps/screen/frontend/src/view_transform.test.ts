import { describe, expect, it } from "vitest";
import {
  IDENTITY_TRANSFORM,
  clampTransform,
  fittedVideoRect,
  panBy,
  screenPointFromClient,
  screenPointFromStagePoint,
  zoomAbout,
} from "./view_transform";

const SCREEN = { width: 1920, height: 1080 };

describe("coordinate mapping", () => {
  it("letterboxes a wide stage and maps the drawn video's corners onto the Screen", () => {
    const stage = { width: 1000, height: 1000 };
    const fitted = fittedVideoRect(stage, SCREEN);
    expect(fitted).not.toBeNull();
    expect(fitted!.left).toBeCloseTo(0);
    expect(fitted!.top).toBeCloseTo(218.75);
    expect(fitted!.width).toBeCloseTo(1000);
    expect(fitted!.height).toBeCloseTo(562.5);
    expect(screenPointFromStagePoint({ x: 0, y: 218.75 }, stage, SCREEN)).toEqual({ x: 0, y: 0 });
    expect(screenPointFromStagePoint({ x: 999, y: 780 }, stage, SCREEN)).toEqual({ x: 1918, y: 1078 });
    expect(screenPointFromStagePoint({ x: 500, y: 500 }, stage, SCREEN)).toEqual({ x: 960, y: 540 });
  });

  it("answers null in the letterbox bars and outside the stage", () => {
    const stage = { width: 1000, height: 1000 };
    expect(screenPointFromStagePoint({ x: 500, y: 100 }, stage, SCREEN)).toBeNull();
    expect(screenPointFromStagePoint({ x: -5, y: 500 }, stage, SCREEN)).toBeNull();
    expect(screenPointFromStagePoint({ x: 500, y: 500 }, stage, { width: 0, height: 0 })).toBeNull();
  });

  it("maps client coordinates through the stage's bounding rect", () => {
    const rect = { left: 100, top: 50, width: 960, height: 540 };
    expect(screenPointFromClient(100 + 480, 50 + 270, rect, SCREEN)).toEqual({ x: 960, y: 540 });
    expect(screenPointFromClient(100 + 959, 50 + 539, rect, SCREEN)).toEqual({ x: 1918, y: 1078 });
  });

  it("undoes a zoom and pan so the point under the finger is the point on the Screen", () => {
    const stage = { width: 960, height: 540 };
    // Zoomed 2x about the stage origin and panned so the Screen's centre sits at the stage's centre.
    const transform = { zoom: 2, pan: { x: -480, y: -270 } };
    expect(screenPointFromStagePoint({ x: 480, y: 270 }, stage, SCREEN, transform)).toEqual({ x: 960, y: 540 });
    expect(screenPointFromStagePoint({ x: 0, y: 0 }, stage, SCREEN, transform)).toEqual({ x: 480, y: 270 });
  });
});

describe("the view transform", () => {
  const stage = { width: 400, height: 300 };

  it("is identity at zoom 1 whatever the pan asked for", () => {
    expect(clampTransform({ zoom: 1, pan: { x: -50, y: 30 } }, stage)).toEqual(IDENTITY_TRANSFORM);
  });

  it("clamps the zoom and keeps the drawn video covering the stage", () => {
    expect(clampTransform({ zoom: 9, pan: { x: 0, y: 0 } }, stage).zoom).toBe(5);
    const panned = panBy({ zoom: 2, pan: { x: 0, y: 0 } }, { x: -1000, y: 50 }, stage);
    expect(panned.pan).toEqual({ x: -400, y: 0 });
  });

  it("zooms about a point, leaving that point where it was", () => {
    const zoomed = zoomAbout(IDENTITY_TRANSFORM, 2, { x: 200, y: 150 }, stage);
    expect(zoomed.zoom).toBe(2);
    expect(zoomed.pan).toEqual({ x: -200, y: -150 });
    const back = zoomAbout(zoomed, 0.5, { x: 200, y: 150 }, stage);
    expect(back).toEqual(IDENTITY_TRANSFORM);
  });
});
