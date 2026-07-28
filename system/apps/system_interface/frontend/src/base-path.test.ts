import { afterEach, describe, expect, it, vi } from "vitest";

// The frontend unit tests run in node (no DOM), so — like Response.test.ts — stub
// document.querySelector to feed the meta tag isCodexEnabled reads. Re-import per case
// (vi.resetModules) because isCodexEnabled caches its result on first read.
function stubEnableCodexMeta(content: string | null): void {
  const element = content === null ? null : ({ getAttribute: () => content } as unknown as Element);
  globalThis.document = { querySelector: () => element } as unknown as Document;
}

describe("isCodexEnabled", () => {
  afterEach(() => vi.resetModules());

  it("is false when the meta tag is absent (default off)", async () => {
    stubEnableCodexMeta(null);
    const { isCodexEnabled } = await import("./base-path");
    expect(isCodexEnabled()).toBe(false);
  });

  it("is true only when the meta tag content is exactly 'true'", async () => {
    stubEnableCodexMeta("true");
    const { isCodexEnabled } = await import("./base-path");
    expect(isCodexEnabled()).toBe(true);
  });

  it("is false when the meta tag content is 'false'", async () => {
    stubEnableCodexMeta("false");
    const { isCodexEnabled } = await import("./base-path");
    expect(isCodexEnabled()).toBe(false);
  });
});
