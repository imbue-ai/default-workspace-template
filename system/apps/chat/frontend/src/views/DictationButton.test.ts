// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import { hoverTooltipText } from "@imbue/workspace-ui/src/testing/tooltip";
import type { DictationProblem, DictationState } from "@imbue/workspace-voice/src/dictation";
import { DictationButton, dictationNotice, dictationProgressPercent, dictationTooltip } from "./DictationButton";
import type { DictationButtonAttrs } from "./DictationButton";

function mountButton(overrides: Partial<DictationButtonAttrs> = {}) {
  const attrs: DictationButtonAttrs = {
    state: "idle",
    progress: null,
    shortcutLabel: "Cmd+Shift+M",
    disabled: false,
    onToggle: vi.fn(),
    onMeter: vi.fn(),
    ...overrides,
  };
  const root = mountView(() => m(DictationButton, attrs));
  m.redraw.sync();
  const button = root.querySelector<HTMLButtonElement>(".message-input-dictate-button")!;
  return { root, button, attrs };
}

afterEach(() => {
  unmountViews();
  vi.useRealTimers();
});

describe("dictationTooltip", () => {
  it("says what the button does in each state, with the shortcut where a press does something", () => {
    expect(dictationTooltip("idle", null, "Cmd+Shift+M")).toBe("Dictate (Cmd+Shift+M)");
    expect(dictationTooltip("listening", null, "Ctrl+Shift+M")).toBe("Stop dictation (Ctrl+Shift+M)");
    expect(dictationTooltip("starting", null, "Cmd+Shift+M")).toBe("Starting dictation");
    expect(dictationTooltip("stopping", null, "Cmd+Shift+M")).toBe("Finishing your words");
    expect(dictationTooltip("loading", null, "Cmd+Shift+M")).toBe("Downloading speech model");
    expect(dictationTooltip("loading", { loadedBytes: 30_000_000, totalBytes: 67_000_000 }, "Cmd+Shift+M")).toBe(
      "Downloading speech model, 44% of 64 MB",
    );
    expect(dictationProgressPercent({ loadedBytes: 5, totalBytes: 0 })).toBe(0);
  });
});

describe("DictationButton", () => {
  it("is a ghost mic beside the send button that starts dictation when pressed", () => {
    vi.useFakeTimers();
    const { button, attrs } = mountButton();
    expect(button.getAttribute("aria-label")).toBe("Dictate");
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(button.dataset.dictationState).toBe("idle");
    expect(button.querySelector("svg.dictation-glyph path.dictation-capsule-fill")).not.toBeNull();
    expect(hoverTooltipText(button)).toBe("Dictate (Cmd+Shift+M)");
    button.click();
    expect(attrs.onToggle).toHaveBeenCalledTimes(1);
  });

  it("wears the neutral pressed tint while listening and fills its capsule to the level it is handed", () => {
    const { button, attrs } = mountButton({ state: "listening" });
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(button.classList.contains("bg-fill-active")).toBe(true);
    expect(button.classList.contains("bg-accent-light")).toBe(false);
    const onMeter = attrs.onMeter as ReturnType<typeof vi.fn>;
    const setLevel = onMeter.mock.calls[0][0] as (level: number) => void;
    const rect = button.querySelector<SVGRectElement>("rect.dictation-level")!;
    expect(rect.getAttribute("height")).toBe("0");
    setLevel(0.5);
    expect(rect.getAttribute("height")).toBe("6.50");
    expect(rect.getAttribute("y")).toBe("8.50");
    setLevel(2);
    expect(rect.getAttribute("height")).toBe("13.00");
  });

  it("draws the download's progress as a ring and waits, and a spinner while starting or stopping", () => {
    const loading = mountButton({ state: "loading", progress: { loadedBytes: 1, totalBytes: 4 } });
    const arc = loading.button.querySelector<SVGCircleElement>(".dictation-ring-arc")!;
    expect(arc.getAttribute("stroke-dasharray")!.startsWith("19.63 ")).toBe(true);
    expect(loading.button.getAttribute("aria-disabled")).toBe("true");
    unmountViews();
    for (const state of ["starting", "stopping"] as DictationState[]) {
      const { button } = mountButton({ state });
      expect(button.querySelector(".spinner-icon")).not.toBeNull();
      expect(button.querySelector(".dictation-glyph")).toBeNull();
      unmountViews();
    }
  });

  it("gives the meter back when the glyph leaves", () => {
    const { attrs } = mountButton({ state: "listening" });
    unmountViews();
    const onMeter = attrs.onMeter as ReturnType<typeof vi.fn>;
    expect(onMeter.mock.calls[onMeter.mock.calls.length - 1][0]).toBeNull();
  });
});

describe("dictationNotice", () => {
  function mountNotice(problem: DictationProblem) {
    const actions = { onRetry: vi.fn(), onDiscard: vi.fn(), onDismiss: vi.fn() };
    const root = mountView(() => dictationNotice(problem, actions));
    m.redraw.sync();
    const labels = Array.from(root.querySelectorAll("button")).map((button) => button.textContent);
    return { root, actions, labels };
  }

  it("offers Retry and Discard for held words, Retry and Dismiss where trying again can help, else Dismiss", () => {
    const held = mountNotice({ kind: "held", message: "Some words are held.", canRetry: true });
    expect(held.labels).toEqual(["Retry", "Discard"]);
    held.root.querySelector<HTMLButtonElement>(".message-input-dictation-discard")!.click();
    expect(held.actions.onDiscard).toHaveBeenCalledTimes(1);
    unmountViews();
    const busy = mountNotice({ kind: "busy", message: "The microphone is in use.", canRetry: true });
    expect(busy.labels).toEqual(["Retry", "Dismiss"]);
    busy.root.querySelector<HTMLButtonElement>(".message-input-dictation-retry")!.click();
    expect(busy.actions.onRetry).toHaveBeenCalledTimes(1);
    unmountViews();
    const blocked = mountNotice({ kind: "blocked", message: "The microphone is blocked.", canRetry: false });
    expect(blocked.labels).toEqual(["Dismiss"]);
    expect(blocked.root.querySelector("[role=alert]")!.textContent).toContain("The microphone is blocked.");
  });
});
