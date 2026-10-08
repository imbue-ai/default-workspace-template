// @vitest-environment jsdom
/**
 * The wait line: collapsed it counts the pending tasks and says how long the oldest has run, pressed it lists
 * each task, and its clocks tick once a second only while it is on screen.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import type { BackgroundTask } from "../models/Chats";
import { BackgroundTasksLine, formatElapsed } from "./BackgroundTasksLine";

const NOW = Date.parse("2026-10-08T12:10:00Z");

function task(id: string, description: string, startedAt: string): BackgroundTask {
  return { id, source: "run_in_background", kind: "", description, started_at: startedAt };
}

let root: HTMLElement;
let tasks: BackgroundTask[] = [];

function mount(): void {
  m.mount(root, { view: () => m(BackgroundTasksLine, { tasks }) });
}

function summaryText(): string {
  return root.querySelector(".background-tasks-line__summary")?.textContent ?? "";
}

function rows(): string[] {
  return [...root.querySelectorAll(".background-tasks-line__task")].map((row) => row.textContent ?? "");
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  root = document.createElement("div");
  document.body.appendChild(root);
});

afterEach(() => {
  m.mount(root, null);
  root.remove();
  vi.useRealTimers();
});

describe("BackgroundTasksLine", () => {
  it("counts the tasks and times the oldest, collapsed until pressed, then lists each task", () => {
    tasks = [
      task("b", "Migrate the schema", "2026-10-08T12:08:30Z"),
      task("a", "Rebuild the worker image", "2026-10-08T12:05:55Z"),
    ];
    mount();

    expect(summaryText()).toBe("Waiting on 2 background tasks· 4m 05s");
    expect(rows()).toEqual([]);
    expect(root.querySelector("button")?.getAttribute("aria-expanded")).toBe("false");

    root.querySelector<HTMLButtonElement>("button")?.click();
    m.redraw.sync();

    expect(root.querySelector("button")?.getAttribute("aria-expanded")).toBe("true");
    expect(rows()).toEqual(["Migrate the schema1m 30s", "Rebuild the worker image4m 05s"]);
  });

  it("names one task in the singular, and a task with no description generically", () => {
    tasks = [task("a", "", "2026-10-08T12:09:58Z")];
    mount();
    expect(summaryText()).toBe("Waiting on 1 background task· 2s");

    root.querySelector<HTMLButtonElement>("button")?.click();
    m.redraw.sync();
    expect(rows()).toEqual(["Background task2s"]);
  });

  it("ticks its clock every second while it is shown, and stops when it goes", () => {
    tasks = [task("a", "Rebuild the worker image", "2026-10-08T12:09:00Z")];
    mount();
    expect(summaryText()).toContain("· 1m 00s");

    vi.advanceTimersByTime(1_000);
    m.redraw.sync();
    expect(summaryText()).toContain("· 1m 01s");

    m.mount(root, null);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("leaves out a time it cannot read rather than inventing one", () => {
    tasks = [task("a", "Rebuild the worker image", "not a time")];
    mount();
    expect(summaryText()).toBe("Waiting on 1 background task");
  });
});

describe("formatElapsed", () => {
  it("reads at a glance's precision", () => {
    expect(formatElapsed(42_000)).toBe("42s");
    expect(formatElapsed(245_000)).toBe("4m 05s");
    expect(formatElapsed((2 * 60 + 7) * 60_000 + 30_000)).toBe("2h 07m");
    // A browser clock behind the workspace's never shows a negative time.
    expect(formatElapsed(-5_000)).toBe("0s");
  });
});
