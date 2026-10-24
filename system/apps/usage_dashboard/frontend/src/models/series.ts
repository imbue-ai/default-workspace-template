/**
 * Which chart series gets which colour. A series keeps its colour for as long as the page is open, whatever the
 * window or filter, so a model never changes colour when another one drops out; the slots are handed out in the
 * order series are first seen, and past the last slot a series folds into "Other".
 */

export const SERIES_SLOTS = 7;
export const OTHER_SERIES = "Other";

export class SeriesColors {
  private readonly slots = new Map<string, number>();

  /** The 1-based colour slot of each series in ``ranked`` (largest first) to draw, then "Other" for the rest. */
  assign(ranked: readonly string[]): { shown: string[]; slotOf: (series: string) => number | null } {
    const shown: string[] = [];
    for (const series of ranked) {
      if (!this.slots.has(series) && this.slots.size < SERIES_SLOTS) this.slots.set(series, this.slots.size + 1);
      if (this.slots.has(series)) shown.push(series);
    }
    return { shown, slotOf: (series) => this.slots.get(series) ?? null };
  }
}

/** The series of the chart: each of ``shown`` by name, anything else as "Other". */
export function seriesOf(name: string, shown: readonly string[]): string {
  return shown.includes(name) ? name : OTHER_SERIES;
}
