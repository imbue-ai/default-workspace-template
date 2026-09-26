/**
 * The shared geometry vectors (desktop-interface contracts.md section 10), which the Python
 * editor's ``desktop_document_test.py`` runs too: a rule is changed by changing the JSON first.
 */
import { readFileSync } from "fs";
import { describe, expect, it } from "vitest";
import type { Frame, GridCell, WindowState } from "../model/records";
import {
  MAXIMIZED_FRAME,
  SNAPPED_LEFT_FRAME,
  SNAPPED_RIGHT_FRAME,
  cascadeFrame,
  clampFrameIntoUnitSquare,
  fitFrameToBackdrop,
  snapZoneForRelease,
  unsnapFrame,
} from "./frames";
import { gridDimensions, nearestFreeCell, placeShortcuts, readingOrderCell } from "./grid";

const VECTORS_PATH = new URL(
  "../../../../../../docs/system/blueprint/desktop-interface/geometry_vectors.json",
  import.meta.url,
).pathname;

interface Size {
  width: number;
  height: number;
}

interface Vectors {
  cascade: { placed_count: number; expected: Frame }[];
  clamp_frame: { frame: Frame; expected: Frame }[];
  snap_frames: Record<WindowState, Frame>;
  fit_metrics: {
    window_min_width: number;
    window_min_height: number;
    title_min_visible: number;
    title_bar_height: number;
  };
  fit: { name: string; frame: Frame; backdrop: Size; expected: Frame }[];
  snap_threshold: number;
  snap_zone: { pointer: { x: number; y: number }; backdrop: Size; expected: WindowState | null }[];
  unsnap: {
    name: string;
    kept_frame: Frame;
    pointer: { x: number; y: number };
    grab_fraction: number;
    grab_offset_y: number;
    expected: Frame;
  }[];
  grid_metrics: { cell_width: number; cell_height: number; inset: number };
  grid_dimensions: { backdrop: Size; expected: { columns: number; rows: number } }[];
  reading_order: { index: number; columns: number; expected: GridCell }[];
  nearest_free_cell: {
    name: string;
    target: GridCell;
    occupied: GridCell[];
    grid: { columns: number; rows: number };
    expected: GridCell;
  }[];
  shortcut_placement: {
    name: string;
    grid: { columns: number; rows: number };
    cells: GridCell[];
    expected: GridCell[];
  }[];
}

const vectors = JSON.parse(readFileSync(VECTORS_PATH, "utf-8")) as Vectors;

function expectFrameClose(actual: Frame, expected: Frame): void {
  expect(actual.x).toBeCloseTo(expected.x, 9);
  expect(actual.y).toBeCloseTo(expected.y, 9);
  expect(actual.width).toBeCloseTo(expected.width, 9);
  expect(actual.height).toBeCloseTo(expected.height, 9);
}

describe("the shared geometry vectors", () => {
  it("cascade", () => {
    for (const vector of vectors.cascade) expectFrameClose(cascadeFrame(vector.placed_count), vector.expected);
  });

  it("clamp_frame", () => {
    for (const vector of vectors.clamp_frame) {
      const { x, y, width, height } = vector.frame;
      expectFrameClose(clampFrameIntoUnitSquare(x, y, width, height), vector.expected);
    }
  });

  it("snap_frames", () => {
    expect(SNAPPED_LEFT_FRAME).toEqual(vectors.snap_frames.SNAPPED_LEFT);
    expect(SNAPPED_RIGHT_FRAME).toEqual(vectors.snap_frames.SNAPPED_RIGHT);
    expect(MAXIMIZED_FRAME).toEqual(vectors.snap_frames.MAXIMIZED);
  });

  it("fit", () => {
    const metrics = {
      windowMinWidth: vectors.fit_metrics.window_min_width,
      windowMinHeight: vectors.fit_metrics.window_min_height,
      titleMinVisible: vectors.fit_metrics.title_min_visible,
      titleBarHeight: vectors.fit_metrics.title_bar_height,
    };
    for (const vector of vectors.fit) {
      expect(fitFrameToBackdrop(vector.frame, vector.backdrop, metrics), vector.name).toEqual(vector.expected);
    }
  });

  it("snap_zone", () => {
    for (const vector of vectors.snap_zone) {
      expect(snapZoneForRelease(vector.pointer, vector.backdrop, vectors.snap_threshold)).toBe(vector.expected);
    }
  });

  it("unsnap", () => {
    for (const vector of vectors.unsnap) {
      expectFrameClose(
        unsnapFrame(vector.kept_frame, vector.pointer, vector.grab_fraction, vector.grab_offset_y),
        vector.expected,
      );
    }
  });

  it("grid_dimensions", () => {
    const metrics = {
      cellWidth: vectors.grid_metrics.cell_width,
      cellHeight: vectors.grid_metrics.cell_height,
      gridInset: vectors.grid_metrics.inset,
    };
    for (const vector of vectors.grid_dimensions) {
      expect(gridDimensions(vector.backdrop, metrics)).toEqual(vector.expected);
    }
  });

  it("reading_order", () => {
    for (const vector of vectors.reading_order) {
      expect(readingOrderCell(vector.index, vector.columns)).toEqual(vector.expected);
    }
  });

  it("nearest_free_cell", () => {
    for (const vector of vectors.nearest_free_cell) {
      expect(nearestFreeCell(vector.target, vector.occupied, vector.grid), vector.name).toEqual(vector.expected);
    }
  });

  it("shortcut_placement", () => {
    for (const vector of vectors.shortcut_placement) {
      const shortcuts = vector.cells.map((cell, index) => ({
        target: { kind: "launch" as const, app: `app-${index}`, launch: "new" },
        mode: "focus" as const,
        cell,
      }));
      expect(
        placeShortcuts(shortcuts, vector.grid).map((placed) => placed.cell),
        vector.name,
      ).toEqual(vector.expected);
    }
  });
});
