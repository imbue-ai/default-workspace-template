import { describe, expect, it } from "vitest";
import { windowRecord } from "../testing/records";
import { navigationsToFollow } from "./following";
import type { PageReport } from "./following";

const windows = [
  windowRecord("win-1", "docs", "/?doc=2"),
  windowRecord("win-2", "notes", "/n"),
  windowRecord("win-3", "docs", "/x"),
];

describe("navigationsToFollow", () => {
  it("navigates a capable page, reloads the rest, and leaves a page already there alone", () => {
    const reports = new Map<string, PageReport>([
      ["win-1", { lastReportedPath: "/?doc=1", isNavigationCapable: true }],
      ["win-2", { lastReportedPath: "/m", isNavigationCapable: false }],
      ["win-3", { lastReportedPath: "/x", isNavigationCapable: true }],
    ]);
    expect(navigationsToFollow(windows, reports)).toEqual([
      { windowId: "win-1", path: "/?doc=2", mode: "navigate" },
      { windowId: "win-2", path: "/n", mode: "reload" },
    ]);
  });

  it("skips windows this client has no page for", () => {
    expect(navigationsToFollow(windows, new Map())).toEqual([]);
  });

  it("moves a page told nothing yet to the stored path", () => {
    const reports = new Map<string, PageReport>([["win-2", { lastReportedPath: null, isNavigationCapable: false }]]);
    expect(navigationsToFollow(windows, reports)).toEqual([{ windowId: "win-2", path: "/n", mode: "reload" }]);
  });
});
