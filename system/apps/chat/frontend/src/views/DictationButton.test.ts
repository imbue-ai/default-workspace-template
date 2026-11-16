// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import { hoverTooltipText } from "@imbue/workspace-ui/src/testing/tooltip";
import type { DictationProblem } from "@imbue/workspace-voice/src/dictation";
import { DictationButton, dictationNotice, dictationProgressPercent, dictationTooltip } from "./DictationButton";
import type { DictationButtonAttrs, DictationButtonState } from "./DictationButton";

const EVERY_STATE: DictationButtonState[] = ["load-needed", "loading", "ready", "starting", "listening", "stopping"];

function mountButton(overrides: Partial<DictationButtonAttrs> = {}) {
  const attrs: DictationButtonAttrs = {
    state: "ready",
    progress: null,
    download: null,
    shortcutLabel: "Cmd+Shift+M",
    disabled: false,
    onPress: vi.fn(),
    onDismissLoading: vi.fn(),
    onMeter: vi.fn(),
    ...overrides,
  };
  const root = mountView(() => m(DictationButton, attrs));
  m.redraw.sync();
  const button = root.querySelector<HTMLButtonElement>(".message-input-dictate-button")!;
  const label = root.querySelector<HTMLButtonElement>(".dictation-morph-label")!;
  return { root, button, label, attrs };
}

afterEach(() => {
  unmountViews();
  vi.useRealTimers();
});

describe("dictationTooltip", () => {
  it("says what the button does in each state, with the shortcut where a press does something", () => {
    expect(dictationTooltip("ready", null, "Cmd+Shift+M")).toBe("Dictate (Cmd+Shift+M)");
    expect(dictationTooltip("listening", null, "Ctrl+Shift+M")).toBe("Stop dictation (Ctrl+Shift+M)");
    expect(dictationTooltip("starting", null, "Cmd+Shift+M")).toBe("Starting dictation");
    expect(dictationTooltip("stopping", null, "Cmd+Shift+M")).toBe("Finishing your words");
    expect(dictationTooltip("loading", null, "Cmd+Shift+M")).toBe("Downloading speech model");
    expect(dictationTooltip("loading", { loadedBytes: 30_000_000, totalBytes: 67_000_000 }, "Cmd+Shift+M")).toBe(
      "Downloading speech model, 44% of 64 MB",
    );
    expect(dictationProgressPercent({ loadedBytes: 5, totalBytes: 0 })).toBe(0);
  });

  it("says what a press sets up while the model is not loaded: a download, sized when known, or a load", () => {
    expect(dictationTooltip("load-needed", null, "Cmd+Shift+M", { totalBytes: 250_000_000 })).toBe(
      "Set up dictation: one-time 238 MB download (Cmd+Shift+M)",
    );
    expect(dictationTooltip("load-needed", null, "Ctrl+Shift+M", { totalBytes: null })).toBe(
      "Set up dictation: one-time download (Ctrl+Shift+M)",
    );
    expect(dictationTooltip("load-needed", null, "Cmd+Shift+M", null)).toBe(
      "Set up dictation: load the speech model (Cmd+Shift+M)",
    );
    // A download still to come changes no other state's words.
    expect(dictationTooltip("ready", null, "Cmd+Shift+M", { totalBytes: 1 })).toBe("Dictate (Cmd+Shift+M)");
    expect(dictationTooltip("listening", null, "Cmd+Shift+M", { totalBytes: 1 })).toBe("Stop dictation (Cmd+Shift+M)");
  });
});

describe("DictationButton", () => {
  it("is a ghost mic beside the send button that starts dictation when pressed, once the model is loaded", () => {
    vi.useFakeTimers();
    const { button, attrs } = mountButton();
    expect(button.getAttribute("aria-label")).toBe("Dictate");
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(button.dataset.dictationState).toBe("ready");
    expect(button.querySelector("svg.dictation-glyph path.dictation-capsule-fill")).not.toBeNull();
    expect(hoverTooltipText(button)).toBe("Dictate (Cmd+Shift+M)");
    button.click();
    expect(attrs.onPress).toHaveBeenCalledTimes(1);
    expect(attrs.onDismissLoading).not.toHaveBeenCalled();
  });

  it("names its state, and says what it is and what a press does, in every state", () => {
    const said: Record<DictationButtonState, { ariaLabel: string; tooltip: string }> = {
      "load-needed": {
        ariaLabel: "Set up dictation",
        tooltip: "Set up dictation: load the speech model (Cmd+Shift+M)",
      },
      loading: { ariaLabel: "Downloading the speech model", tooltip: "Downloading speech model" },
      ready: { ariaLabel: "Dictate", tooltip: "Dictate (Cmd+Shift+M)" },
      starting: { ariaLabel: "Starting dictation", tooltip: "Starting dictation" },
      listening: { ariaLabel: "Stop dictation", tooltip: "Stop dictation (Cmd+Shift+M)" },
      stopping: { ariaLabel: "Finishing your words", tooltip: "Finishing your words" },
    };
    for (const state of EVERY_STATE) {
      const { button } = mountButton({ state });
      expect(button.dataset.dictationState).toBe(state);
      expect(button.getAttribute("aria-label")).toBe(said[state].ariaLabel);
      expect(button.getAttribute("data-hover-tooltip")).toBe(said[state].tooltip);
      unmountViews();
    }
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
    for (const state of ["starting", "stopping"] as DictationButtonState[]) {
      const { button } = mountButton({ state });
      expect(button.querySelector(".spinner-icon")).not.toBeNull();
      expect(button.querySelector(".dictation-glyph")).toBeNull();
      expect(button.getAttribute("aria-disabled")).toBe("true");
      unmountViews();
    }
  });

  it("asks for the model first, as a faint mic with a warning badge, while the model is not loaded", () => {
    vi.useFakeTimers();
    const { button, attrs } = mountButton({ state: "load-needed", download: { totalBytes: 250_000_000 } });
    expect(button.getAttribute("aria-label")).toBe("Set up dictation");
    expect(button.querySelector("svg.dictation-glyph")!.classList.contains("text-faint")).toBe(true);
    const badge = button.querySelector<HTMLElement>(".dictation-setup-badge")!;
    expect(badge.classList.contains("text-warning")).toBe(true);
    expect(badge.querySelector("svg")).not.toBeNull();
    expect(hoverTooltipText(button)).toBe("Set up dictation: one-time 238 MB download (Cmd+Shift+M)");
    button.click();
    expect(attrs.onPress).toHaveBeenCalledTimes(1);
    unmountViews();
    const unsized = mountButton({ state: "load-needed", download: { totalBytes: null } });
    expect(hoverTooltipText(unsized.button)).toBe("Set up dictation: one-time download (Cmd+Shift+M)");
    unmountViews();
    // The files are on this device already: the same look, and a press that loads them.
    const downloaded = mountButton({ state: "load-needed", download: null });
    expect(hoverTooltipText(downloaded.button)).toBe("Set up dictation: load the speech model (Cmd+Shift+M)");
    expect(downloaded.button.querySelector(".dictation-setup-badge")).not.toBeNull();
    expect(downloaded.button.querySelector("svg.dictation-glyph")!.classList.contains("text-faint")).toBe(true);
  });

  it("wears the badge and the faint glyph in no other state, whatever is still to download", () => {
    for (const state of EVERY_STATE.filter((each) => each !== "load-needed")) {
      const { button } = mountButton({ state, download: { totalBytes: 250_000_000 } });
      expect(button.querySelector(".dictation-setup-badge"), state).toBeNull();
      expect(button.querySelector(".text-faint"), state).toBeNull();
      unmountViews();
    }
  });

  it("eases into a label with the download's progress while the model's load is shown", () => {
    const { root, button, label } = mountButton({
      state: "loading",
      progress: { loadedBytes: 1, totalBytes: 4 },
      download: { totalBytes: 4 },
    });
    const morph = root.querySelector<HTMLElement>(".dictation-morph")!;
    expect(morph.hasAttribute("data-dictation-loading")).toBe(true);
    expect(morph.classList.contains("grid-cols-[1fr]")).toBe(true);
    expect(label.textContent).toBe("Loading voice model");
    expect(morph.querySelector<HTMLElement>(".dictation-morph-bar")!.style.width).toBe("25%");
    // The mic fades under the label and keeps its ring; the label is the signal now, so the badge goes.
    expect(morph.querySelector<HTMLElement>(".dictation-morph-mic")!.classList.contains("opacity-0")).toBe(true);
    expect(button.querySelector(".dictation-ring-arc")).not.toBeNull();
    expect(button.querySelector(".dictation-setup-badge")).toBeNull();
  });

  it("makes the label the button that dismisses the load, and says so", () => {
    vi.useFakeTimers();
    const { label, attrs } = mountButton({ state: "loading", progress: { loadedBytes: 1, totalBytes: 4 } });
    expect(label.tagName).toBe("BUTTON");
    expect(label.type).toBe("button");
    expect(label.disabled).toBe(false);
    expect(label.hasAttribute("tabindex")).toBe(false);
    expect(label.hasAttribute("aria-hidden")).toBe(false);
    expect(label.getAttribute("aria-label")).toBe("Keep typing while the speech model loads");
    expect(hoverTooltipText(label)).toBe("Downloading speech model, 25% of 4 B. Click to keep typing.");
    label.click();
    expect(attrs.onDismissLoading).toHaveBeenCalledTimes(1);
    expect(attrs.onPress).not.toHaveBeenCalled();
    unmountViews();
    const unsized = mountButton({ state: "loading" });
    expect(hoverTooltipText(unsized.label)).toBe("Downloading speech model. Click to keep typing.");
  });

  it("keeps the collapsed label out of reach at rest", () => {
    for (const state of EVERY_STATE.filter((each) => each !== "loading")) {
      const { root, label, attrs } = mountButton({ state });
      const morph = root.querySelector<HTMLElement>(".dictation-morph")!;
      expect(morph.hasAttribute("data-dictation-loading"), state).toBe(false);
      expect(morph.classList.contains("grid-cols-[0fr]"), state).toBe(true);
      expect(morph.querySelector<HTMLElement>(".dictation-morph-mic")!.classList.contains("opacity-0"), state).toBe(
        false,
      );
      expect(label.disabled, state).toBe(true);
      expect(label.getAttribute("tabindex"), state).toBe("-1");
      expect(label.getAttribute("aria-hidden"), state).toBe("true");
      expect(label.hasAttribute("data-hover-tooltip"), state).toBe(false);
      label.click();
      expect(attrs.onDismissLoading, state).not.toHaveBeenCalled();
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
