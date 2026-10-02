// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, describe, expect, it } from "vitest";

import m from "mithril";
import { buttonNamed } from "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import type { HistoryRange, HistoryState } from "../models/history";
import { HOUR_END_SECONDS, closure, historyView, period } from "../testing/records";
import { HistoryChart } from "./HistoryChart";

const START = HOUR_END_SECONDS - 3600;

function mountChart(state: HistoryState, onRange: (range: HistoryRange) => void = () => {}): HTMLElement {
  return mountView(() => m(HistoryChart, { state, range: "HOUR", onRange }));
}

describe("the memory-over-time chart", () => {
  afterEach(unmountViews);

  it("draws one line per unbroken run of readings and the line where closing starts", () => {
    const root = mountChart({
      kind: "loaded",
      view: historyView({
        periods: [period(START, 100, 90, 110), period(START + 60, 120, 100, 140), period(START + 600, 200, 200, 200)],
      }),
    });
    expect(root.querySelectorAll("polyline")).toHaveLength(1);
    expect(root.querySelectorAll("circle")).toHaveLength(1);
    expect(root.querySelectorAll("polygon.fill-accent\\/15")).toHaveLength(2);
    expect(root.textContent).toContain("Closing starts");
    expect(root.textContent).not.toContain("Nothing recorded yet");
  });

  it("marks each closure in range and lists the week's closures in plain words with what to do next", () => {
    const closed = closure("2026-10-01T11:30:00Z", 'the agent of "Wallpaper"', 512 * 1024);
    const root = mountChart({
      kind: "loaded",
      view: historyView({
        periods: [period(START, 100, 90, 110)],
        closures_in_range: [closed],
        recent_closures: [closed],
      }),
    });
    expect(root.querySelectorAll(".activity-closure-mark")).toHaveLength(1);
    expect(root.textContent).toContain('Closed the agent of "Wallpaper", freeing about 512 MB');
    expect(root.textContent).toContain("send it a message to carry on");
  });

  it("says when nothing was closed, and that the system's own closures at its limit go unrecorded", () => {
    const root = mountChart({ kind: "loaded", view: historyView({ closer: "SYSTEM_LIMIT" }) });
    expect(root.textContent).toContain("Nothing has been closed to free memory in the last week.");
    expect(root.textContent).toContain("those closures aren't recorded");
  });

  it("explains an empty chart before the first reading, and a pause in recording", () => {
    const empty = mountChart({
      kind: "loaded",
      view: historyView({ is_recording: false, first_sample_epoch_seconds: null }),
    });
    expect(empty.textContent).toContain("Nothing recorded yet.");
    unmountViews();

    const paused = mountChart({
      kind: "loaded",
      view: historyView({ is_recording: false, periods: [period(START, 100, 90, 110)] }),
    });
    expect(paused.textContent).toContain("Recording has paused");
  });

  it("shows the readings as a table and where they are kept, on request", () => {
    const root = mountChart({
      kind: "loaded",
      view: historyView({ periods: [period(START, 100, 90, 110), period(START + 60, 120, 100, 140)] }),
    });
    expect(root.querySelector("table")).toBeNull();
    buttonNamed(root, "Show the readings").click();
    m.redraw.sync();
    expect(root.querySelectorAll("tbody tr")).toHaveLength(2);
    buttonNamed(root, "How this is recorded").click();
    m.redraw.sync();
    expect(root.textContent).toContain("data/.state/activity/memory-history.tsv");
    expect(root.textContent).toContain("data/.state/oom_priority/events/shed.jsonl");
  });

  it("asks for the range the user picks", () => {
    const picked: HistoryRange[] = [];
    const root = mountChart({ kind: "loading" }, (range) => picked.push(range));
    expect(root.textContent).toContain("Reading the history");
    buttonNamed(root, "Last 7 days").click();
    expect(picked).toEqual(["WEEK"]);
    expect(buttonNamed(root, "Last hour").getAttribute("aria-pressed")).toBe("true");
  });
});
