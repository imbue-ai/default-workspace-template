// @vitest-environment jsdom
import m from "mithril";
import { describe, expect, it, vi } from "vitest";
import type { MemoryChange, MemoryChangeCardState } from "./memory-change-card";
import { lineDiff, renderMemoryChangeCard } from "./memory-change-card";
import { supersededMemoryChangeNotices } from "./turn-grouping";
import type { TranscriptEvent } from "../models/Response";

const CHANGE: MemoryChange = {
  id: "c1",
  time: "2026-11-18T22:00:00+00:00",
  summary: "added note 'tea' and its index line",
  chat_id: "chat-1",
  tool_use_ids: ["t1", "t2"],
  files: [
    {
      name: "MEMORY.md",
      before: "# Index\n- [A](a.md)",
      after: "# Index\n- [A](a.md)\n- [Tea](tea.md)",
      label: "Your index (the list of notes I read at the start of every chat)",
      before_text: "Index\nA",
      after_text: "Index\nA\nTea — likes tea",
      note_id: "",
      details_url: null,
    },
    {
      name: "tea.md",
      before: null,
      after: "---\nname: tea\n---\nlikes tea",
      label: "New note: Tea",
      before_text: null,
      after_text: "Summary: Likes tea\n\nThe user likes tea.",
      note_id: "tea",
      details_url: "http://memory-abc.localhost/?note=tea",
    },
  ],
  undone_at: null,
  details_url: "http://memory-abc.localhost/?note=tea",
};

function draw(state: Partial<MemoryChangeCardState>, onUndo = vi.fn(), onToggle = vi.fn()): HTMLElement {
  const root = document.createElement("div");
  const full: MemoryChangeCardState = {
    summary: CHANGE.summary,
    change: CHANGE,
    isExpanded: false,
    isBusy: false,
    error: "",
    ...state,
  };
  m.render(root, renderMemoryChangeCard(full, { onUndo, onToggle }));
  return root;
}

describe("lineDiff", () => {
  it("marks added and removed lines and folds distant unchanged ones", () => {
    const before = ["a", "b", "c", "d", "e"].join("\n");
    const after = ["a", "b", "c", "d", "E"].join("\n");
    expect(lineDiff(before, after)).toEqual([
      ["fold", "3 unchanged lines"],
      ["same", "d"],
      ["removed", "e"],
      ["added", "E"],
    ]);
  });

  it("shows a new file as all added and a deleted one as all removed", () => {
    expect(lineDiff(null, "x\ny")).toEqual([
      ["added", "x"],
      ["added", "y"],
    ]);
    expect(lineDiff("x", null)).toEqual([["removed", "x"]]);
  });
});

describe("renderMemoryChangeCard", () => {
  it("names the change and offers Undo, with the lines hidden until asked for", () => {
    const root = draw({});
    expect(root.textContent).toContain("Memory changed: added note 'tea' and its index line");
    expect(root.querySelector(".memory-change-undo")).not.toBeNull();
    expect(root.querySelector(".memory-change-diff")).toBeNull();
  });

  it("shows each file's changed lines as a person reads them, without the note's bookkeeping", () => {
    const root = draw({ isExpanded: true });
    const files = [...root.querySelectorAll(".memory-change-file")].map((f) => f.textContent ?? "");
    expect(files[0]).toContain("Your index");
    expect(files[0]).toContain("Tea — likes tea");
    expect(files[1]).toContain("New note: Tea");
    expect(files[1]).toContain("Summary: Likes tea");
    expect(files[1]).toContain("The user likes tea.");
    const added = [...root.querySelectorAll(".memory-change-file")[1].querySelectorAll(".diff-added")].map(
      (l) => l.textContent,
    );
    expect(added).toEqual(["Summary: Likes tea", "\u00a0", "The user likes tea."]);
    expect(files[1]).not.toContain("name:");
  });

  it("links to the note in the Memory app", () => {
    const link = draw({}).querySelector(".memory-change-details") as HTMLAnchorElement;
    expect(link.textContent).toBe("View details");
    expect(link.getAttribute("href")).toBe("http://memory-abc.localhost/?note=tea");
  });

  it("calls Undo when clicked", () => {
    const onUndo = vi.fn();
    const root = draw({}, onUndo);
    (root.querySelector(".memory-change-undo") as HTMLButtonElement).click();
    expect(onUndo).toHaveBeenCalledOnce();
  });

  it("drops Undo once the change is undone", () => {
    const root = draw({ change: { ...CHANGE, undone_at: "2026-11-18T22:05:00+00:00" } });
    expect(root.textContent).toContain("Memory change undone:");
    expect(root.querySelector(".memory-change-undo")).toBeNull();
  });

  it("shows the notice's summary before the change has loaded, with no actions yet", () => {
    const root = draw({ change: null });
    expect(root.textContent).toContain("Memory changed: added note 'tea' and its index line");
    expect(root.querySelector(".memory-change-undo")).toBeNull();
  });
});

describe("supersededMemoryChangeNotices", () => {
  const notice = (eventId: string, changeId: string) =>
    ({ type: "user_message", event_id: eventId, memory_change_id: changeId }) as unknown as TranscriptEvent;

  it("keeps only the last notice of each change", () => {
    const events = [notice("e1", "c1"), notice("e2", "c1"), notice("e3", "c2")];
    expect([...supersededMemoryChangeNotices(events)]).toEqual(["e1"]);
  });
});
