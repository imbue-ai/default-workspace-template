import { describe, expect, it } from "vitest";
import { shouldReloadForBuild } from "./reload";

describe("shouldReloadForBuild", () => {
  it("reloads when the served build id differs from the loaded one", () => {
    expect(shouldReloadForBuild("abc123", "def456")).toBe(true);
  });

  it("does not reload when the build ids match", () => {
    expect(shouldReloadForBuild("abc123", "abc123")).toBe(false);
  });

  it("does not reload when the page has no build id (e.g. dev server)", () => {
    expect(shouldReloadForBuild("", "def456")).toBe(false);
  });

  it("does not reload when the server reports no build id (e.g. bundle missing)", () => {
    expect(shouldReloadForBuild("abc123", "")).toBe(false);
  });
});
