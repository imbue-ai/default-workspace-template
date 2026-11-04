import { describe, expect, it } from "vitest";

import { logPathOf, pageOf, pathOf, routinePathOf, runPathOf, titleOf } from "./routing";

describe("pageOf", () => {
  it("names the list, a routine and a run from the path", () => {
    expect(pageOf("/")).toEqual({ kind: "list" });
    expect(pageOf("/inbox-digest")).toEqual({ kind: "routine", routineId: "inbox-digest" });
    expect(pageOf("/inbox-digest/runs/run-abc")).toEqual({ kind: "run", routineId: "inbox-digest", runId: "run-abc" });
    expect(pageOf("/inbox-digest/runs/run-abc/log")).toEqual({
      kind: "log",
      routineId: "inbox-digest",
      runId: "run-abc",
    });
  });

  it("falls back to the list for a path it does not know", () => {
    expect(pageOf("/inbox-digest/other")).toEqual({ kind: "list" });
    expect(pageOf("/inbox-digest/runs/run-abc/other")).toEqual({ kind: "list" });
    expect(pageOf("/a/b/c/d")).toEqual({ kind: "list" });
  });

  it("round-trips the paths it builds", () => {
    expect(pageOf(routinePathOf("news-sync"))).toEqual({ kind: "routine", routineId: "news-sync" });
    expect(pageOf(runPathOf("news-sync", "run-1"))).toEqual({ kind: "run", routineId: "news-sync", runId: "run-1" });
    expect(pageOf(logPathOf("news-sync", "run-1"))).toEqual({ kind: "log", routineId: "news-sync", runId: "run-1" });
    for (const path of ["/", "/news-sync", "/news-sync/runs/run-1", "/news-sync/runs/run-1/log"]) {
      expect(pathOf(pageOf(path))).toBe(path);
    }
  });
});

describe("titleOf", () => {
  it("uses the routine's title once known and its id before", () => {
    expect(titleOf({ kind: "list" }, null)).toBe("Routines");
    expect(titleOf({ kind: "routine", routineId: "x" }, null)).toBe("x");
    expect(titleOf({ kind: "routine", routineId: "x" }, "News sync")).toBe("News sync");
    expect(titleOf({ kind: "run", routineId: "x", runId: "r" }, "News sync")).toBe("News sync: a run");
    expect(titleOf({ kind: "log", routineId: "x", runId: "r" }, "News sync")).toBe("News sync: a run's log");
  });
});
