import { describe, expect, it } from "vitest";

import { shouldReloadForEpoch } from "./reload";

describe("shouldReloadForEpoch", () => {
  it("reloads a page that loaded before the current reveal", () => {
    // The case the epoch exists for: the page was open (or its socket was
    // down) when a reveal landed, so the reload broadcast reached nobody.
    expect(shouldReloadForEpoch("epoch-2", "epoch-1")).toBe(true);
  });

  it("leaves a page that already loaded the current epoch alone", () => {
    // Also what stops a reload loop: the reloaded document carries the new
    // epoch, so the very next connect must be a no-op.
    expect(shouldReloadForEpoch("epoch-2", "epoch-2")).toBe(false);
  });

  it("does not reload when the workspace has never been revealed", () => {
    // No epoch on disk yet. Treating that as a change would reload every page
    // in a fresh workspace on connect.
    expect(shouldReloadForEpoch("", "")).toBe(false);
    expect(shouldReloadForEpoch("", "epoch-1")).toBe(false);
  });

  it("reloads a page served before the epoch existed at all", () => {
    // The shell predates this mechanism (or was served by an older build), so
    // it carries no epoch while the server now reports one.
    expect(shouldReloadForEpoch("epoch-1", "")).toBe(true);
  });
});
