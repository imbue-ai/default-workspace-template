import { describe, expect, it } from "vitest";

import { modelMark, providerMark } from "./providerMarks";

describe("modelMark", () => {
  it("marks a model with its vendor's mark at the asked size", () => {
    const mark = modelMark("claude-opus-4-7", 16);
    expect(mark).toContain('width="16"');
    expect(mark).not.toContain("{S}");
    expect(mark).toBe(providerMark("anthropic", 16));
  });

  it("marks the static Claude aliases as Anthropic", () => {
    expect(modelMark("opus[1m]", 16)).toBe(providerMark("anthropic", 16));
    expect(modelMark("haiku", 16)).toBe(providerMark("anthropic", 16));
  });

  it("finds the vendor of a routed id by its last segment", () => {
    expect(modelMark("moonshotai/kimi-k2-6", 16)).not.toBe("");
    expect(modelMark("moonshotai/kimi-k2-6", 16)).toBe(modelMark("kimi-k2-6", 16));
  });

  it("gives an unknown vendor no mark rather than a wrong one", () => {
    expect(modelMark("grok-4.7", 16)).toBe("");
    expect(modelMark("muse-spark-1.3", 16)).toBe("");
  });
});
