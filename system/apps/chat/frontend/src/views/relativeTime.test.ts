// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Age, longAge, shortAge } from "./relativeTime";

const NOW = Date.parse("2026-10-27T12:00:00Z");
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

describe("shortAge", () => {
  it.each([
    [0, "now"],
    [59_000, "now"],
    [5 * MINUTE, "5m"],
    [23 * HOUR + 59 * MINUTE, "23h"],
    [DAY, "1d"],
    [29 * DAY, "29d"],
    [65 * DAY, "2mo"],
    [400 * DAY, "1y"],
    [-5 * MINUTE, "now"],
  ])("writes an age of %i ms as %s", (elapsed, expected) => {
    expect(shortAge(NOW - elapsed, NOW)).toBe(expected);
  });
});

describe("longAge", () => {
  it.each([
    [30_000, "just now"],
    [MINUTE, "1 minute ago"],
    [5 * HOUR, "5 hours ago"],
    [DAY, "1 day ago"],
    [3 * DAY + 2 * HOUR, "3 days ago"],
  ])("writes an age of %i ms as %s", (elapsed, expected) => {
    expect(longAge(NOW - elapsed, NOW)).toBe(expected);
  });
});

describe("Age", () => {
  let root: HTMLElement | null = null;

  afterEach(() => {
    vi.useRealTimers();
    if (root === null) return;
    m.mount(root, null);
    root.remove();
    root = null;
  });

  it("keeps its text current as time passes, with no redraw of the page", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    root = document.createElement("div");
    document.body.appendChild(root);
    m.mount(root, {
      view: () => m(Age, { atMs: NOW - 30_000, format: shortAge, tooltipLead: "Last message", class: "age" }),
    });
    const age = root.querySelector(".age");
    expect(age?.textContent).toBe("now");
    expect(age?.getAttribute("title")).toMatch(/^Last message /);

    vi.advanceTimersByTime(2 * MINUTE);
    expect(age?.textContent).toBe("2m");
  });

  it("shows a new time at once, after its timer has rewritten the text", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    root = document.createElement("div");
    document.body.appendChild(root);
    let atMs = NOW - 3 * DAY;
    m.mount(root, { view: () => m(Age, { atMs, format: shortAge, tooltipLead: "Last message", class: "age" }) });
    vi.advanceTimersByTime(MINUTE);
    expect(root.querySelector(".age")?.textContent).toBe("3d");

    atMs = Date.now();
    m.redraw.sync();
    expect(root.querySelector(".age")?.textContent).toBe("now");
  });
});
