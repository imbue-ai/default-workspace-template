import { describe, expect, it } from "vitest";

import { OTHER_SERIES, SERIES_SLOTS, SeriesColors, seriesOf } from "./series";

describe("SeriesColors", () => {
  it("keeps a series' colour when the ranking changes", () => {
    const colors = new SeriesColors();
    const first = colors.assign(["a", "b", "c"]);
    const second = colors.assign(["c", "a"]);

    expect(["a", "b", "c"].map(first.slotOf)).toEqual([1, 2, 3]);
    expect(["c", "a"].map(second.slotOf)).toEqual([3, 1]);
  });

  it("folds series past the last slot into Other", () => {
    const names = Array.from({ length: SERIES_SLOTS + 2 }, (_, index) => `model-${index}`);
    const { shown } = new SeriesColors().assign(names);

    expect(shown).toHaveLength(SERIES_SLOTS);
    expect(seriesOf(names[SERIES_SLOTS], shown)).toBe(OTHER_SERIES);
  });
});
