import { describe, expect, it } from "vitest";

import type { ProjectInfo } from "./Inventory";
import { EVERYTHING_VIEW_ID, chooseInitialViewId, isEverythingView, projectForViewId, searchRows } from "./Projects";

function project(id: string, name: string): ProjectInfo {
  return { id, name, color: "#111111", glyph: 0, tabs: [], shortcuts: [] };
}

describe("chooseInitialViewId", () => {
  const projects = [project("alpha", "Alpha"), project("beta", "Beta")];

  it("keeps the stored view when it still exists", () => {
    expect(chooseInitialViewId(projects, "beta")).toBe("beta");
  });

  it("keeps Everything when that is where the client was", () => {
    expect(chooseInitialViewId(projects, EVERYTHING_VIEW_ID)).toBe(EVERYTHING_VIEW_ID);
  });

  it("falls back to the first project, then to Everything", () => {
    expect(chooseInitialViewId(projects, "gone")).toBe("alpha");
    expect(chooseInitialViewId([], "gone")).toBe(EVERYTHING_VIEW_ID);
  });
});

describe("views", () => {
  it("tells Everything from a project and names both", () => {
    const projects = [project("alpha", "Alpha")];
    expect(isEverythingView(EVERYTHING_VIEW_ID)).toBe(true);
    expect(projectForViewId(projects, "alpha")?.name).toBe("Alpha");
    expect(projectForViewId(projects, EVERYTHING_VIEW_ID)).toBeNull();
  });
});

describe("searchRows", () => {
  const rows = [
    { label: "Terminal 1", kindWords: ["terminal", "Terminal"] },
    { label: "Docs", kindWords: ["files", "Files"] },
    { label: "Alice", kindWords: ["chat", "Chat"] },
  ];

  it("keeps everything with nothing bolded on an empty query", () => {
    expect(searchRows(rows, "  ").map((result) => result.labelRanges)).toEqual([[], [], []]);
  });

  it("matches labels case-insensitively and reports where", () => {
    const results = searchRows(rows, "doc");
    expect(results.map((result) => result.row.label)).toEqual(["Docs"]);
    expect(results[0].labelRanges).toEqual([{ start: 0, end: 3 }]);
  });

  it("keeps rows whose kind words match, with nothing to bold", () => {
    const results = searchRows(rows, "chat");
    expect(results.map((result) => result.row.label)).toEqual(["Alice"]);
    expect(results[0].labelRanges).toEqual([]);
  });
});
