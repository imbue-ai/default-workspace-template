import { afterEach, describe, expect, it } from "vitest";

import { firstRunProvidersUrl, isFirstRunProviderStepPending, setFirstRunProviderStep } from "./firstRunCover";

describe("the first run's cover", () => {
  afterEach(() => {
    setFirstRunProviderStep(false, "");
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
});
