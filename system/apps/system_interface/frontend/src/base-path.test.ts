import { afterEach, describe, expect, it, vi } from "vitest";

// The frontend unit tests run in node (no DOM), so — like Response.test.ts — stub
// document.querySelector to feed the meta tag areOtherHarnessesEnabled reads. Re-import
// per case (vi.resetModules) because it caches its result on first read.
function stubEnableOtherHarnessesMeta(content: string | null): void {
  const element = content === null ? null : ({ getAttribute: () => content } as unknown as Element);
  globalThis.document = { querySelector: () => element } as unknown as Document;
}

describe("areOtherHarnessesEnabled", () => {
  afterEach(() => vi.resetModules());

  it("is false when the meta tag is absent (default off)", async () => {
    stubEnableOtherHarnessesMeta(null);
    const { areOtherHarnessesEnabled } = await import("./base-path");
    expect(areOtherHarnessesEnabled()).toBe(false);
  });

  it("is true only when the meta tag content is exactly 'true'", async () => {
    stubEnableOtherHarnessesMeta("true");
    const { areOtherHarnessesEnabled } = await import("./base-path");
    expect(areOtherHarnessesEnabled()).toBe(true);
  });

  it("is false when the meta tag content is 'false'", async () => {
    stubEnableOtherHarnessesMeta("false");
    const { areOtherHarnessesEnabled } = await import("./base-path");
    expect(areOtherHarnessesEnabled()).toBe(false);
  });
});
