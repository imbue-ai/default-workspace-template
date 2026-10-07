// @vitest-environment jsdom
/**
 * The wait line under the activity strip: collapsed it counts the pending tasks and says how long
 * the oldest has run, pressed it lists each task, and its clock ticks only while tasks are pending.
 */
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BackgroundTask } from "../models/Chats";
import { BackgroundTasksLine } from "./BackgroundTasksLine";

const NOW = Date.parse("2026-10-06T12:00:00Z");

function startedSecondsAgo(seconds: number): string {
  return new Date(NOW - seconds * 1000).toISOString();
}

const REBUILD: BackgroundTask = {
  id: "t-1",
  description: "Rebuild the worker image",
  started_at: startedSecondsAgo(4 * 60 + 10),
};
const MIGRATE: BackgroundTask = { id: "t-2", description: "Migrate the schema", started_at: startedSecondsAgo(30) };

let root: HTMLElement;

function render(tasks: readonly BackgroundTask[]): void {
  m.render(root, m(BackgroundTasksLine, { tasks }));
}

function summaryText(): string | null {
  return root.querySelector(".background-tasks-line__summary")?.textContent ?? null;
}

function taskRows(): string[] {
  return [...root.querySelectorAll(".background-tasks-line__task")].map((row) => row.textContent ?? "");
}

function pressSummary(tasks: readonly BackgroundTask[]): void {
  root.querySelector<HTMLButtonElement>(".background-tasks-line__summary")?.click();
  render(tasks);
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(NOW);
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  root = document.createElement("div");
});

afterEach(() => {
  m.render(root, null);
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("BackgroundTasksLine", () => {
  it("starts collapsed, counting the tasks and timing the oldest", () => {
    render([REBUILD, MIGRATE]);

    expect(summaryText()).toBe("Waiting on 2 background tasks· 4 min");
    expect(root.querySelector(".background-tasks-line__summary")?.getAttribute("aria-expanded")).toBe("false");
    expect(taskRows()).toEqual([]);
  });

  it("says a single task in the singular", () => {
    render([MIGRATE]);

    expect(summaryText()).toBe("Waiting on a background task· 30 sec");
  });

  it("lists each task's description and elapsed time when pressed, and folds back up when pressed again", () => {
    const tasks = [REBUILD, { ...MIGRATE, started_at: startedSecondsAgo(2 * 3600 + 5 * 60) }];
    render(tasks);

    pressSummary(tasks);

    expect(root.querySelector(".background-tasks-line__summary")?.getAttribute("aria-expanded")).toBe("true");
    expect(taskRows()).toEqual(["Rebuild the worker image· 4 min", "Migrate the schema· 2 hr 5 min"]);
    // The line offers nothing to press but the summary: no per-task controls.
    expect(root.querySelectorAll("button")).toHaveLength(1);

    pressSummary(tasks);

    expect(taskRows()).toEqual([]);
  });

  it("ticks the elapsed time once a second while tasks are pending, and stops when they are gone", () => {
    const tasks = [{ ...MIGRATE, started_at: startedSecondsAgo(59) }];
    render(tasks);
    expect(vi.getTimerCount()).toBe(1);
    expect(summaryText()).toBe("Waiting on a background task· 59 sec");

    vi.advanceTimersByTime(1000);
    expect(m.redraw).toHaveBeenCalledTimes(1);
    render(tasks);
    expect(summaryText()).toBe("Waiting on a background task· 1 min");

    render([]);
    expect(vi.getTimerCount()).toBe(0);
    expect(root.textContent).toBe("");
    vi.advanceTimersByTime(5000);
    expect(m.redraw).toHaveBeenCalledTimes(1);
  });

  it("stops its clock when it is taken off the page", () => {
    render([REBUILD]);
    expect(vi.getTimerCount()).toBe(1);

    m.render(root, null);

    expect(vi.getTimerCount()).toBe(0);
  });
});
