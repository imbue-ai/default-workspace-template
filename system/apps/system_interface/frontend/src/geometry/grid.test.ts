import { describe, expect, it } from "vitest";
import { cellAtPoint, cellRect, drawnCells, firstFreeCellInReadingOrder, placeShortcuts } from "./grid";

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

describe("placeShortcuts", () => {
  it("answers the drawn cells in shortcut order", () => {
    const shortcuts = [
      {
        target: { kind: "launch" as const, app: "a", launch: "new" },
        mode: "focus" as const,
        cell: { column: 0, row: 0 },
      },
      {
        target: { kind: "launch" as const, app: "b", launch: "new" },
        mode: "new" as const,
        cell: { column: 0, row: 0 },
      },
    ];
    const placed = placeShortcuts(shortcuts, GRID);
    expect(placed.map((entry) => entry.shortcut.target.app)).toEqual(["a", "b"]);
    expect(drawnCells(placed)).toEqual([
      { column: 0, row: 0 },
      { column: 0, row: 1 },
    ]);
  });
});
