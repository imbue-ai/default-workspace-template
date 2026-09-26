/**
 * The shortcut grid (desktop-interface contracts.md section 10): how many cells the backdrop
 * holds, reading order, the nearest free cell, and the render-time placement of shortcuts,
 * mirroring ``shell/desktop_document.py``. Nothing here reads the DOM.
 */

import { isSameCell } from "../model/records";
import type { DesktopShortcut, GridCell } from "../model/records";
import type { PixelPoint, PixelRect, PixelSize } from "./frames";

/** The theme metrics the grid rule reads. */
export interface GridMetrics {
  readonly cellWidth: number;
  readonly cellHeight: number;
  readonly gridInset: number;
}

export interface GridDimensions {
  readonly columns: number;
  readonly rows: number;
}

/** A shortcut with the cell it is drawn in, which may differ from its stored cell. */
export interface PlacedShortcut {
  readonly shortcut: DesktopShortcut;
  readonly cell: GridCell;
}

export function gridDimensions(backdrop: PixelSize, metrics: GridMetrics): GridDimensions {
  return {
    columns: Math.max(1, Math.floor((backdrop.width - metrics.gridInset) / metrics.cellWidth)),
    rows: Math.max(1, Math.floor((backdrop.height - metrics.gridInset) / metrics.cellHeight)),
  };
}

export function readingOrderCell(index: number, columns: number): GridCell {
  return { column: index % columns, row: Math.floor(index / columns) };
}

export function clampCellIntoGrid(cell: GridCell, dimensions: GridDimensions): GridCell {
  return {
    column: Math.min(Math.max(cell.column, 0), dimensions.columns - 1),
    row: Math.min(Math.max(cell.row, 0), dimensions.rows - 1),
  };
}

export function isCellInsideGrid(cell: GridCell, dimensions: GridDimensions): boolean {
  return cell.column >= 0 && cell.row >= 0 && cell.column < dimensions.columns && cell.row < dimensions.rows;
}

/** The squared Euclidean distance between two cells: exact in integers, so a genuine tie compares equal
 *  (a rounded ``Math.hypot`` need not, and the shell's editor decides the same ties). */
function squaredCellDistance(first: GridCell, second: GridCell): number {
  const columns = first.column - second.column;
  const rows = first.row - second.row;
  return columns * columns + rows * rows;
}

/** Whether ``candidate`` is nearer the target than ``best`` (ties by lower column, then lower row). */
function isNearer(candidate: GridCell, best: GridCell, target: GridCell): boolean {
  const candidateDistance = squaredCellDistance(candidate, target);
  const bestDistance = squaredCellDistance(best, target);
  if (candidateDistance !== bestDistance) return candidateDistance < bestDistance;
  if (candidate.column !== best.column) return candidate.column < best.column;
  return candidate.row < best.row;
}

function isOccupied(cell: GridCell, occupied: readonly GridCell[]): boolean {
  return occupied.some((taken) => isSameCell(taken, cell));
}

/**
 * The nearest free cell to the target clamped into the grid (ties by lower column then lower row);
 * a grid with no free cell answers the clamped target itself.
 */
export function nearestFreeCell(
  target: GridCell,
  occupied: readonly GridCell[],
  dimensions: GridDimensions,
): GridCell {
  const clamped = clampCellIntoGrid(target, dimensions);
  let best: GridCell | null = null;
  for (let column = 0; column < dimensions.columns; column += 1) {
    for (let row = 0; row < dimensions.rows; row += 1) {
      const candidate = { column, row };
      if (isOccupied(candidate, occupied)) continue;
      if (best === null || isNearer(candidate, best, clamped)) best = candidate;
    }
  }
  return best ?? clamped;
}

/** The first free cell in reading order over a grid ``columns`` wide (unbounded below). */
export function firstFreeCellInReadingOrder(occupied: readonly GridCell[], columns: number): GridCell {
  let index = 0;
  while (isOccupied(readingOrderCell(index, columns), occupied)) index += 1;
  return readingOrderCell(index, columns);
}

/** The render-time placement: every shortcut whose stored cell is inside the grid and unclaimed takes it,
 *  in shortcut order; every other shortcut takes the nearest free cell to its clamped stored cell, in order. */
export function placeShortcuts(shortcuts: readonly DesktopShortcut[], dimensions: GridDimensions): PlacedShortcut[] {
  const claimed: GridCell[] = [];
  const placedByIndex = new Map<number, GridCell>();
  shortcuts.forEach((shortcut, index) => {
    if (isCellInsideGrid(shortcut.cell, dimensions) && !isOccupied(shortcut.cell, claimed)) {
      claimed.push(shortcut.cell);
      placedByIndex.set(index, shortcut.cell);
    }
  });
  shortcuts.forEach((shortcut, index) => {
    if (placedByIndex.has(index)) return;
    const cell = nearestFreeCell(shortcut.cell, claimed, dimensions);
    claimed.push(cell);
    placedByIndex.set(index, cell);
  });
  return shortcuts.map((shortcut, index) => {
    const cell = placedByIndex.get(index);
    if (cell === undefined) throw new Error("every shortcut is placed");
    return { shortcut, cell };
  });
}

/** The cells the placed shortcuts draw in. */
export function drawnCells(placed: readonly PlacedShortcut[]): GridCell[] {
  return placed.map((entry) => entry.cell);
}

/** The pixel box a cell draws in. */
export function cellRect(cell: GridCell, metrics: GridMetrics): PixelRect {
  return {
    x: metrics.gridInset + cell.column * metrics.cellWidth,
    y: metrics.gridInset + cell.row * metrics.cellHeight,
    width: metrics.cellWidth,
    height: metrics.cellHeight,
  };
}

/** The cell a backdrop point falls in, clamped into the grid. */
export function cellAtPoint(point: PixelPoint, metrics: GridMetrics, dimensions: GridDimensions): GridCell {
  return clampCellIntoGrid(
    {
      column: Math.floor((point.x - metrics.gridInset) / metrics.cellWidth),
      row: Math.floor((point.y - metrics.gridInset) / metrics.cellHeight),
    },
    dimensions,
  );
}
