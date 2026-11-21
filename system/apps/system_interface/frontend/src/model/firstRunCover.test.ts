import { afterEach, describe, expect, it } from "vitest";

import {
  applyFirstRunProviderStep,
  firstRunProvidersUrl,
  isFirstRunProviderStepPending,
  noteChromeWashesExit,
  setFirstRunProviderStep,
} from "./firstRunCover";
import type { FirstRunChrome } from "./firstRunCover";

/** A chrome that records what it hears and holds what it is asked to run later. */
function recordingChrome(): { chrome: FirstRunChrome; reports: boolean[]; scheduled: [() => void, number][] } {
  const reports: boolean[] = [];
  const scheduled: [() => void, number][] = [];
  return {
    chrome: { report: (isPending) => reports.push(isPending), coverMs: 560, schedule: (run, ms) => scheduled.push([run, ms]) },
    reports,
    scheduled,
  };
}

describe("the first run's cover", () => {
  afterEach(() => {
    setFirstRunProviderStep(false, "");
    noteChromeWashesExit(false, recordingChrome().chrome);
  });

  it("frames the reporting app's own page while the step is owed, and nothing once it is not", () => {
    expect(isFirstRunProviderStepPending()).toBe(false);
    expect(firstRunProvidersUrl()).toBeNull();

    expect(setFirstRunProviderStep(true, "https://chat.example")).toBe(true);
    expect(isFirstRunProviderStepPending()).toBe(true);
    expect(firstRunProvidersUrl()).toBe("https://chat.example/?view=first-run");
    // Told the same thing again: nothing to redraw.
    expect(setFirstRunProviderStep(true, "https://chat.example")).toBe(false);

    expect(setFirstRunProviderStep(false, "https://chat.example")).toBe(true);
    expect(firstRunProvidersUrl()).toBeNull();
  });

  it("tells the chrome the page went up, and takes it down at once for a chrome that does not wash its end", () => {
    const { chrome, reports, scheduled } = recordingChrome();
    let redraws = 0;
    applyFirstRunProviderStep(true, "https://chat.example", chrome, () => redraws++);
    applyFirstRunProviderStep(true, "https://chat.example", chrome, () => redraws++);
    expect(reports).toEqual([true]);
    expect(redraws).toBe(1);

    applyFirstRunProviderStep(false, "https://chat.example", chrome, () => redraws++);
    expect(reports).toEqual([true, false]);
    expect(scheduled).toEqual([]);
    expect(isFirstRunProviderStepPending()).toBe(false);
    expect(redraws).toBe(2);
  });

  it("keeps the page up under a washing chrome's color for its cover time, then takes it down", () => {
    const { chrome, reports, scheduled } = recordingChrome();
    noteChromeWashesExit(true, chrome);
    let redraws = 0;
    applyFirstRunProviderStep(true, "https://chat.example", chrome, () => redraws++);
    applyFirstRunProviderStep(false, "https://chat.example", chrome, () => redraws++);
    // Said again while the color is coming: nothing more is reported or scheduled.
    applyFirstRunProviderStep(false, "https://chat.example", chrome, () => redraws++);
    expect(reports).toEqual([true, false]);
    expect(isFirstRunProviderStepPending()).toBe(true);
    expect(scheduled.map(([, ms]) => ms)).toEqual([560]);

    scheduled[0][0]();
    expect(isFirstRunProviderStepPending()).toBe(false);
    expect(redraws).toBe(2);
  });

  it("tells a chrome that starts listening late about a page already up, and reports no end for a page never up", () => {
    const { chrome, reports } = recordingChrome();
    applyFirstRunProviderStep(false, "https://chat.example", chrome, () => undefined);
    expect(reports).toEqual([]);

    setFirstRunProviderStep(true, "https://chat.example");
    noteChromeWashesExit(true, chrome);
    expect(reports).toEqual([true]);
  });
});
