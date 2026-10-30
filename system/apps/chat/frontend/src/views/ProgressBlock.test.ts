// @vitest-environment jsdom
/**
 * The steps not started yet fold into one "Up next" row at the end of the timeline: collapsed it names the next
 * few in a faint line, pressed it shows them all laid out like a step's revealed work, and the other steps render
 * as before.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import m from "mithril";
import { nextPendingTitles, PENDING_STEPS_LABEL, ProgressBlock } from "./ProgressBlock";
import type { StepNode, StepStatus, TimelineItem } from "./turn-grouping";

function node(ticket_id: string, title: string, status: StepStatus, summary: string | null = null): StepNode {
  return { ticket_id, title, status, summary, narration: null, is_carryover: false, is_frontier: false, events: [] };
}

function step(ticket_id: string, title: string, status: StepStatus, summary: string | null = null): TimelineItem {
  return { kind: "step", step: node(ticket_id, title, status, summary) };
}

let root: HTMLElement;

function mount(items: TimelineItem[], id: string): void {
  m.mount(root, {
    view: () => m(ProgressBlock, { id, items, trailing_reply: [], toolResults: new Map(), chatId: "chat-1" }),
  });
}

function rowTitles(): string[] {
  return [...root.querySelectorAll(".pv-tl-node > div > button")].map((button) =>
    (button.textContent ?? "").replace("›", "").trim(),
  );
}

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
});

afterEach(() => {
  m.mount(root, null);
  root.remove();
});

const MIXED: TimelineItem[] = [
  step("s1", "Read the paper", "done", "Read it"),
  step("s2", "Build the page", "active"),
  step("s3", "Add a quiz", "pending"),
  step("s4", "Check it works", "pending"),
];

describe("the Up next row", () => {
  it("folds the steps not started yet into one row after the others", () => {
    mount(MIXED, "progress-folded");

    expect(rowTitles()).toEqual(["Read the paper", "Build the page", PENDING_STEPS_LABEL]);
    expect(root.querySelector(".pv-tl-pending-next")?.textContent).toBe("Add a quiz, Check it works");
    expect(root.querySelector(".pv-tl-node--pending-group")?.classList.contains("pv-tl-node--last")).toBe(true);
  });

  it("shows each step not started yet below its label when pressed, like a step's revealed work", () => {
    mount(MIXED, "progress-expanded");

    (root.querySelector(".pv-tl-node--pending-group button") as HTMLButtonElement).click();
    m.redraw.sync();

    const inside = root.querySelector(".pv-tl-node--pending-group .pv-tl-expanded .pv-expanded");
    expect([...(inside?.querySelectorAll(".pv-pending-item") ?? [])].map((item) => item.textContent)).toEqual([
      "Add a quiz",
      "Check it works",
    ]);
    expect(inside?.querySelector(".pv-tl-node")).toBeNull();
    expect(root.querySelector(".pv-tl-pending-next")).toBeNull();
  });

  it("is absent when every step has started", () => {
    mount([step("s2", "Build the page", "active")], "progress-none-pending");

    expect(rowTitles()).toEqual(["Build the page"]);
    expect(root.querySelector(".pv-tl-node--pending-group")).toBeNull();
  });
});

describe("the Up next row's subtext", () => {
  it("names the next three in order and ends with an ellipsis when there are more", () => {
    const pending = ["A", "B", "C", "D"].map((title) => node(title, title, "pending"));
    expect(nextPendingTitles(pending)).toBe("A, B, C, …");
    expect(nextPendingTitles(pending.slice(0, 2))).toBe("A, B");
  });
});
