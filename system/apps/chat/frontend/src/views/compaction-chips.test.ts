// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import m from "mithril";
import type { CompactionCause, UserMessageEvent } from "../models/Response";
import { closeRunningCompactionChips, compactionChips, landedCompaction, runningCompaction } from "./compaction-chips";
import { setBlockExpanded } from "./expansion-state";
import { ToolChipGroup } from "./ToolChipGroup";

const EVENT_ID = "evt-compacted";
const SUMMARY = "Summary of earlier conversation across 12 turns.";

function compactionEvent(cause?: CompactionCause | null, summary?: string): UserMessageEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "user_message",
    event_id: EVENT_ID,
    source: "claude",
    role: "system",
    content: "Context was compacted",
    display: "status",
    non_turn_tail: true,
    ...(cause === undefined ? {} : { compaction_cause: cause }),
    ...(summary === undefined ? {} : { display_body: summary }),
  };
}

let root: HTMLElement;

function mount(event: UserMessageEvent | null, cause: CompactionCause | null = null): void {
  const part = event === null ? runningCompaction(cause) : landedCompaction(event, false);
  m.render(root, m(ToolChipGroup, { chips: compactionChips(part), toolResults: new Map(), chatId: "agent-x" }));
}

function chipLabels(): string[] {
  return [...root.querySelectorAll(".tool-chip .tool-chip-label")].map((el) => el.textContent ?? "");
}

/** Open the chip at `index` and return its panel's body: what follows the header. */
function openPanelBody(event: UserMessageEvent | null, index: number): Element[] {
  mount(event);
  root.querySelectorAll<HTMLButtonElement>(".tool-chip")[index].click();
  mount(event);
  const panel = root.querySelector(".tool-chip-detail.compaction-detail");
  expect(panel).not.toBeNull();
  return Array.from(panel!.children).slice(1);
}

beforeEach(() => {
  for (const id of [EVENT_ID, "compaction-running"]) {
    setBlockExpanded(`chip:compaction-started:${id}`, false);
    setBlockExpanded(`chip:compaction-finished:${id}`, false);
  }
  root = document.createElement("div");
  document.body.appendChild(root);
});

describe("a compaction's chips", () => {
  it.each([
    ["manual", "Compacting as requested…", "Compacted as requested"],
    ["idle", "Compacting while idle…", "Compacted while idle"],
    ["native", "Compacting to free up context…", "Compacted to free up context"],
    [null, "Compacting…", "Context was compacted"],
    [undefined, "Compacting…", "Context was compacted"],
  ] as const)("names the start and the finish for cause %s", (cause, started, finished) => {
    mount(compactionEvent(cause, SUMMARY));
    expect(chipLabels()).toEqual([started, finished]);
    const [startChip, finishChip] = root.querySelectorAll(".tool-chip");
    expect(startChip.classList.contains("compaction-chip--started")).toBe(true);
    expect(finishChip.classList.contains("compaction-chip--finished")).toBe(true);
  });

  it("draws package-open on the start chip and package on the finish chip", () => {
    mount(compactionEvent("idle"));
    const [startChip, finishChip] = root.querySelectorAll(".tool-chip");
    // Each glyph's own first path.
    expect(startChip.querySelector("svg path")?.getAttribute("d")).toBe("M12 22v-9");
    expect(finishChip.querySelector("svg polyline")?.getAttribute("points")).toBe("3.29 7 12 12 20.71 7");
  });

  it("shows only the start chip for the compaction running now", () => {
    mount(null, "idle");
    expect(chipLabels()).toEqual(["Compacting while idle…"]);
  });

  it("closes the running compaction's chip, so the next compaction does not open with it", () => {
    openPanelBody(null, 0);
    closeRunningCompactionChips();
    mount(null, "manual");
    expect(chipLabels()).toEqual(["Compacting as requested…"]);
    expect(root.querySelector(".tool-chip-detail")).toBeNull();
  });

  it.each([
    ["idle", "Compacted while idle to keep replies fast and cheap. Change this under Auto-compact in the model menu."],
    ["manual", "Compacted because you asked (/compact)."],
    ["native", "Your agent triggered compaction. You can ask it about its current setting, or tell it to change it."],
    [null, "Compacted to keep replies fast and cheap. Idle compaction is under Auto-compact in the model menu."],
    [undefined, "Compacted to keep replies fast and cheap. Idle compaction is under Auto-compact in the model menu."],
  ] as const)("opens on the explanation for cause %s, then a dashed rule, then the summary", (cause, text) => {
    for (const index of [0, 1]) {
      const [explanation, separated, ...rest] = openPanelBody(compactionEvent(cause, SUMMARY), index);
      expect(explanation.classList.contains("compaction-explanation")).toBe(true);
      expect(explanation.querySelector("p")?.textContent).toBe(text);
      expect(separated.className).toContain("border-t border-dashed");
      expect(separated.querySelector(".compaction-summary")?.textContent).toBe(SUMMARY);
      expect(rest).toEqual([]);
    }
  });

  it("titles each chip's panel with that chip's label and glyph", () => {
    mount(compactionEvent("manual", SUMMARY));
    root.querySelectorAll<HTMLButtonElement>(".tool-chip")[0].click();
    mount(compactionEvent("manual", SUMMARY));
    let header = root.querySelector(".tool-chip-detail-header")!;
    expect(header.querySelector(".tool-chip-detail-title")?.textContent).toBe("Compacting as requested…");
    expect(header.querySelector("svg path")?.getAttribute("d")).toBe("M12 22v-9");

    root.querySelectorAll<HTMLButtonElement>(".tool-chip")[1].click();
    mount(compactionEvent("manual", SUMMARY));
    header = root.querySelector(".tool-chip-detail-header")!;
    expect(header.querySelector(".tool-chip-detail-title")?.textContent).toBe("Compacted as requested");
    expect(header.querySelector("svg polyline")).not.toBeNull();
  });

  it("opens on the explanation alone when there is no summary yet", () => {
    for (const event of [compactionEvent("idle"), null]) {
      const body = openPanelBody(event, 0);
      expect(body).toHaveLength(1);
      expect(body[0].classList.contains("compaction-explanation")).toBe(true);
      expect(root.querySelector(".compaction-summary")).toBeNull();
    }
  });

  it("clamps a long summary the way a tool's output is clamped", () => {
    const long = Array.from({ length: 40 }, (_, i) => `summary line ${i + 1}`).join("\n");
    openPanelBody(compactionEvent("native", long), 1);
    expect(root.querySelector(".compaction-summary")?.textContent).not.toContain("summary line 21");
    expect(root.querySelector(".tool-call-output-toggle")?.textContent).toBe("View all 40 lines");
  });

  it("sets the explanation in the helper text style, with /compact as an inline-code chip", () => {
    openPanelBody(compactionEvent("manual", SUMMARY), 1);
    const explanation = root.querySelector(".compaction-explanation");
    expect(explanation?.classList.contains("markdown-content")).toBe(true);
    const code = explanation?.querySelector("p > code");
    expect(code?.textContent).toBe("/compact");
    const textStyle = code?.closest("p")?.parentElement;
    expect(textStyle?.classList.contains("text-(length:--font-size-helper)")).toBe(true);
  });
});
