// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, describe, expect, it } from "vitest";

import m from "mithril";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import type { StorageState, StorageSummary } from "../models/storage";
import { buttonNamed } from "../testing/dom";
import { StoragePage } from "./StoragePage";

const SUMMARY: StorageSummary = {
  measured_at: "2026-10-01T12:00:00Z",
  measure_seconds: 1.2,
  total_kib: 4 * 1024 * 1024,
  categories: [
    {
      category_id: "files",
      name: "Your files",
      description: "What you keep under data/",
      size_kib: 3 * 1024 * 1024,
      folders: [{ path: "/home/user/workspace/data/uploads", size_kib: 3 * 1024 * 1024 }],
    },
    { category_id: "logs", name: "Logs", description: "What programs wrote", size_kib: 1024 * 1024, folders: [] },
  ],
  largest: [{ path: "/home/user/workspace/data/uploads", size_kib: 3 * 1024 * 1024, category_name: "Your files" }],
  command: "du -sk ...",
  notes: [],
};

function mountPage(state: StorageState, onMeasure: () => void = () => {}): HTMLElement {
  return mountView(() => m(StoragePage, { state, onMeasure }));
}

describe("the storage tab", () => {
  afterEach(unmountViews);

  it("shows each kind's size and share of the total, and its folders on request", () => {
    const root = mountPage({ kind: "loaded", summary: SUMMARY });
    expect(root.textContent).toContain("Your files");
    expect(root.textContent).toContain("3.0 GB · 75%");
    buttonNamed(root, "Folders").click();
    m.redraw.sync();
    expect(root.querySelector(".activity-details")?.textContent).toContain("/home/user/workspace/data/uploads");
  });

  it("keeps the last measurement on screen while measuring again", () => {
    const root = mountPage({ kind: "measuring", previous: SUMMARY });
    expect(root.textContent).toContain("Your files");
    expect(buttonNamed(root, "Measuring…").disabled).toBe(true);
  });

  it("says when measuring failed and offers to try again", () => {
    let measured = 0;
    const root = mountPage({ kind: "failed", message: "du timed out" }, () => (measured += 1));
    expect(root.textContent).toContain("Couldn't measure what's on disk. du timed out");
    buttonNamed(root, "Try again").click();
    expect(measured).toBe(1);
  });
});
