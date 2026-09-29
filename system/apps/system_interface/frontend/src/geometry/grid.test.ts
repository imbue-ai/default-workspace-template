import { describe, expect, it } from "vitest";
import {
  cellAtPoint,
  cellRect,
  drawnCells,
  firstFreeCellInReadingOrder,
  nearestFreeCell,
  placeShortcuts,
  withRoomMadeFor,
} from "./grid";
import type { PlacedShortcut } from "./grid";
import { shortcutRecord } from "../testing/records";
import type { GridCell } from "../model/records";

const METRICS = { cellWidth: 96, cellHeight: 112, gridInset: 16 };
const GRID = { columns: 4, rows: 3 };

describe("cells and pixels", () => {
  it("draws a cell at the inset plus its offset", () => {
    expect(cellRect({ column: 2, row: 1 }, METRICS)).toEqual({ x: 208, y: 128, width: 96, height: 112 });
  });

  it("finds the cell under a point, clamped into the grid", () => {
    expect(cellAtPoint({ x: 250, y: 130 }, METRICS, GRID)).toEqual({ column: 2, row: 1 });
    expect(cellAtPoint({ x: 5, y: 5 }, METRICS, GRID)).toEqual({ column: 0, row: 0 });
    expect(cellAtPoint({ x: 5000, y: 5000 }, METRICS, GRID)).toEqual({ column: 3, row: 2 });
  });
});

describe("firstFreeCellInReadingOrder", () => {
  it("skips the taken cells", () => {
    const occupied = [
      { column: 0, row: 0 },
      { column: 1, row: 0 },
      { column: 0, row: 1 },
    ];
    expect(firstFreeCellInReadingOrder(occupied, 2)).toEqual({ column: 1, row: 1 });
    expect(firstFreeCellInReadingOrder([], 3)).toEqual({ column: 0, row: 0 });
  });
});

describe("nearestFreeCell", () => {
  it("decides a genuine tie by lower column then lower row, however hypot rounds it", () => {
    // Every cell nearer than sqrt(85) to the origin is taken; (2,9) and (6,7) tie at exactly sqrt(85).
    const occupied: { column: number; row: number }[] = [];
    for (let column = 0; column < 10; column += 1) {
      for (let row = 0; row < 10; row += 1) {
        if (column * column + row * row < 85) occupied.push({ column, row });
      }
    }
    expect(nearestFreeCell({ column: 0, row: 0 }, occupied, { columns: 10, rows: 10 })).toEqual({ column: 2, row: 9 });
  });

  it("answers the clamped target itself when no cell is free", () => {
    const occupied = [
      { column: 0, row: 0 },
      { column: 1, row: 0 },
    ];
    expect(nearestFreeCell({ column: 5, row: 5 }, occupied, { columns: 2, rows: 1 })).toEqual({ column: 1, row: 0 });
  });
});

describe("placeShortcuts", () => {
  it("answers the drawn cells in shortcut order", () => {
    const shortcuts = [shortcutRecord("a", { column: 0, row: 0 }), shortcutRecord("b", { column: 0, row: 0 })];
    const placed = placeShortcuts(shortcuts, GRID);
    expect(placed.map((entry) => entry.shortcut.target.app)).toEqual(["a", "b"]);
    expect(drawnCells(placed)).toEqual([
      { column: 0, row: 0 },
      { column: 0, row: 1 },
    ]);
  });
});

describe("withRoomMadeFor", () => {
  const WIDE = { columns: 6, rows: 4 };

  /** Shortcuts at the given cells, named "a", "b", ... in the order they are listed. */
  function at(...cells: readonly [number, number][]): PlacedShortcut[] {
    return cells.map(([column, row], index) => {
      const cell = { column, row };
      return { shortcut: shortcutRecord(String.fromCharCode(97 + index), cell), cell };
    });
  }

  /** The room made, as a cell per app name. */
  function roomMade(
    placed: readonly PlacedShortcut[],
    held: string,
    target: GridCell,
    dimensions = GRID,
  ): Record<string, [number, number]> {
    const made = withRoomMadeFor(placed, `${held}:new`, target, dimensions);
    return Object.fromEntries(
      made.map((entry) => [entry.shortcut.target.app, [entry.cell.column, entry.cell.row]]),
    ) as Record<string, [number, number]>;
  }

  it("moves nothing else when the cell it is held over is free", () => {
    expect(roomMade(at([0, 0], [0, 1]), "a", { column: 2, row: 2 })).toEqual({ a: [2, 2], b: [0, 1] });
  });

  it("sends the shortcut it is held over to the nearest free cell", () => {
    expect(roomMade(at([1, 2], [3, 1]), "a", { column: 3, row: 1 }, WIDE)).toEqual({ a: [3, 1], b: [2, 1] });
  });

  it("sends it to the same cell whichever side the drag arrived from", () => {
    for (const from of [
      [5, 1],
      [3, 3],
      [0, 0],
    ] as const) {
      expect(roomMade(at([...from], [3, 1]), "a", { column: 3, row: 1 }, WIDE)).toEqual({ a: [3, 1], b: [2, 1] });
    }
  });

  it("leaves every shortcut that is merely between the two alone", () => {
    expect(roomMade(at([0, 0], [0, 1], [3, 1], [1, 2]), "d", { column: 0, row: 0 }, WIDE)).toEqual({
      a: [1, 0],
      b: [0, 1],
      c: [3, 1],
      d: [0, 0],
    });
  });

  it("can step into the cell the held shortcut is leaving, which is free by then", () => {
    expect(roomMade(at([1, 1], [2, 1], [3, 1]), "a", { column: 2, row: 1 }, WIDE)).toEqual({
      a: [2, 1],
      b: [1, 1],
      c: [3, 1],
    });
  });

  it("answers the placement unchanged when the held shortcut is already in that cell or is not placed", () => {
    const placed = at([0, 0], [1, 0]);
    expect(roomMade(placed, "a", { column: 0, row: 0 })).toEqual({ a: [0, 0], b: [1, 0] });
    expect(roomMade(placed, "zz", { column: 2, row: 2 })).toEqual({ a: [0, 0], b: [1, 0] });
  });

  it("clamps the cell it is held over into the grid", () => {
    expect(roomMade(at([0, 0]), "a", { column: 9, row: 9 })).toEqual({ a: [3, 2] });
  });
});
