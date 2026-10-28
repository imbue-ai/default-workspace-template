// @vitest-environment jsdom
/**
 * The timeline's done steps fold into one "Done" row: collapsed it hides them, pressed it shows each with its
 * summary, and open and waiting steps render below it either way.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import m from "mithril";
import { DONE_STEPS_LABEL, ProgressBlock, recentDoneTitles } from "./ProgressBlock";
import type { StepNode, StepStatus, TimelineItem } from "./turn-grouping";

function step(ticket_id: string, title: string, status: StepStatus, summary: string | null = null): TimelineItem {
  const node: StepNode = { ticket_id, title, status, summary, narration: null, is_frontier: false, events: [] };
  return { kind: "step", step: node };
}

let root: HTMLElement;

function mount(items: TimelineItem[], id: string): void {
  m.mount(root, {
    view: () => m(ProgressBlock, { id, items, trailing_reply: [], toolResults: new Map(), chatId: "chat-1" }),
  });
}

function rowTitles(): string[] {
  return [...root.querySelectorAll(".pv-tl-node button")].map((button) =>
    (button.textContent ?? "").replace("›", "").trim(),
  );
}

function summaries(): string[] {
  return [...root.querySelectorAll(".pv-tl-summary")].map((summary) => summary.textContent ?? "");
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
  step("s2", "Write the summary", "done", "Wrote it"),
  step("s3", "Build the page", "active"),
  step("s4", "Check it works", "pending"),
];

describe("the Done row", () => {
  it("folds the done steps into one row above the open and waiting ones", () => {
    mount(MIXED, "progress-folded");

    expect(rowTitles()).toEqual([DONE_STEPS_LABEL, "Build the page", "Check it works"]);
    expect(summaries()).toEqual([]);
  });

  it("shows each done step with its summary when pressed", () => {
    mount(MIXED, "progress-expanded");

    (root.querySelector(".pv-tl-node--done-group button") as HTMLButtonElement).click();
    m.redraw.sync();

    expect(rowTitles()).toEqual([
      DONE_STEPS_LABEL,
      "Read the paper",
      "Write the summary",
      "Build the page",
      "Check it works",
    ]);
    expect(summaries()).toEqual(["Read it", "Wrote it"]);
  });

  // Like a step's chevron, which shows the step's work under its title inside its own row.
  it("shows the done steps inside its own row, below its label", () => {
    mount(MIXED, "progress-inside");

    (root.querySelector(".pv-tl-node--done-group button") as HTMLButtonElement).click();
    m.redraw.sync();

    const inside = root.querySelector(".pv-tl-node--done-group .pv-tl-expanded");
    expect([...(inside?.querySelectorAll(".pv-tl-node button") ?? [])].map((b) => b.textContent?.trim())).toEqual([
      "Read the paper",
      "Write the summary",
    ]);
  });

  it("is absent from a timeline with no done step", () => {
    mount([step("s3", "Build the page", "active")], "progress-none-done");

    expect(rowTitles()).toEqual(["Build the page"]);
  });

  it("is the whole of a timeline whose steps are all done", () => {
    mount([step("s1", "Read the paper", "done", "Read it")], "progress-all-done");

    expect(rowTitles()).toEqual([DONE_STEPS_LABEL]);
    expect(root.querySelector(".pv-tl-node--done-group")?.classList.contains("pv-tl-node--last")).toBe(true);
  });
});

describe("the Done row's subtext", () => {
  const done = (title: string): StepNode => ({
    ticket_id: title,
    title,
    status: "done",
    summary: null,
    narration: null,
    is_frontier: false,
    events: [],
  });

  it("names the finished steps newest first", () => {
    expect(recentDoneTitles([done("Read"), done("Write")])).toBe("Write, Read");
  });

  it("names the three newest and ends with an ellipsis when there are more", () => {
    expect(recentDoneTitles([done("A"), done("B"), done("C"), done("D")])).toBe("D, C, B, …");
  });

  it("shows under the label while collapsed, and not while the steps are shown", () => {
    mount(MIXED, "progress-subtext");
    const subtext = (): string | null => root.querySelector(".pv-tl-done-recent")?.textContent ?? null;
    expect(subtext()).toBe("Write the summary, Read the paper");

    (root.querySelector(".pv-tl-node--done-group button") as HTMLButtonElement).click();
    m.redraw.sync();

    expect(subtext()).toBeNull();
  });
});
