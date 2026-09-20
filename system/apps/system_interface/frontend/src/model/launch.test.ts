import { describe, expect, it } from "vitest";
import {
  defaultLaunchPathOf,
  launchPathOf,
  launchPathWithParams,
  launchRowLabel,
  launchTilesOf,
  orderLaunchTiles,
  promptTargetOfTiles,
} from "./launch";
import { appRecord, launchPathRecord } from "../testing/records";

const docs = appRecord("docs", {
  launcher_rank: 20,
  launch_paths: [launchPathRecord({ id: "new", label: "New docs", params: ["message"] })],
});
const notes = appRecord("notes", { launcher_rank: 10 });
const plain = appRecord("plain", { launch_paths: [launchPathRecord({ id: "open", label: "Open plain", path: "/" })] });
const hidden = appRecord("hidden", { internal: true });

describe("launch tiles", () => {
  it("lists every launch path of every openable app, ranked apps first", () => {
    const tiles = orderLaunchTiles(launchTilesOf([plain, docs, hidden, notes]));
    expect(tiles.map((tile) => `${tile.app.name}:${tile.launchPath.id}`)).toEqual([
      "notes:new",
      "docs:new",
      "plain:open",
    ]);
  });

  it("sends a seeded prompt to the first ranked launch path that takes a message", () => {
    expect(promptTargetOfTiles(launchTilesOf([plain, docs, notes]))?.app.name).toBe("docs");
    expect(promptTargetOfTiles(launchTilesOf([plain, notes]))).toBeNull();
  });

  it("restates a tile as a row", () => {
    expect(launchRowLabel({ app: docs, launchPath: docs.launch_paths[0] })).toBe("Open new docs");
  });
});

describe("launch paths of an app", () => {
  it("finds a launch path by id, and the default one", () => {
    expect(launchPathOf(docs, "new")?.label).toBe("New docs");
    expect(launchPathOf(docs, "other")).toBeNull();
    expect(defaultLaunchPathOf(docs)?.id).toBe("new");
    expect(defaultLaunchPathOf(appRecord("first", { default_shortcut: null }))?.id).toBe("new");
    expect(defaultLaunchPathOf(appRecord("none", { launch_paths: [], default_shortcut: null }))).toBeNull();
  });

  it("appends params as a query string", () => {
    expect(launchPathWithParams(launchPathRecord(), {})).toBe("/new");
    expect(launchPathWithParams(launchPathRecord(), { message: "hi there", account_id: "a/1" })).toBe(
      "/new?message=hi+there&account_id=a%2F1",
    );
  });
});
