/**
 * The composer's mic (the workspace-voice-input spec, D5): one button that carries the speech model's state and
 * every state of dictation, with no bar and no timer. While the model is not loaded the mic asks for it first:
 * the glyph faint, a warning badge on its corner, and a press that loads the model without listening. While
 * that load is shown the button eases into a label with the download's progress as a bar under it, and a press
 * on the label dismisses it, leaving the load to run on. The label takes over the keyboard's focus when the mic
 * holds it, and the faded mic leaves the tab order. Once the model is loaded a press dictates: a spinner while
 * starting and stopping, and while listening the capsule fills from the bottom with the input level, drawn inside
 * the glyph so nothing in the toolbar moves. The level is set imperatively, through the setter the button hands
 * its owner, since it changes every frame and a redraw per frame would redraw the whole page.
 *
 * Beside it, the notice the composer shows over itself when dictation has something to say, in the style of its
 * provider notice.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { MIC_CAPSULE_PATH, alertDotIcon, iconPaths, spinnerIcon } from "@imbue/workspace-ui/src/components/icons";
import type { DictationProblem } from "@imbue/workspace-voice/src/dictation";
import { formatFileSize } from "../models/attachments";

/**
 * What the mic shows, which is also the value of its ``data-dictation-state``. The first three are the speech
 * model's, as the composer sees it; the rest are dictation's own.
 *
 * - ``load-needed``: the model is not loaded, whether it was never downloaded, is downloaded but not in memory,
 *   or is loading with its load not shown. A press loads it and shows the load.
 * - ``loading``: the model's load is being shown.
 * - ``ready``: the model is loaded. A press dictates.
 * - ``starting``, ``listening``, ``stopping``: a dictation is running.
 */
export type DictationButtonState = "load-needed" | "loading" | "ready" | "starting" | "listening" | "stopping";

export interface DictationProgress {
  loadedBytes: number;
  totalBytes: number;
}

/** The one-time download a first load of the speech model is; ``totalBytes`` is null when its size is not
 *  known. */
export interface DictationDownload {
  totalBytes: number | null;
}

/** The whole-number percent of a download, for the tooltip, the ring and the label's bar. */
export function dictationProgressPercent(progress: DictationProgress | null): number {
  if (progress === null || progress.totalBytes <= 0) return 0;
  return Math.min(100, Math.floor((100 * progress.loadedBytes) / progress.totalBytes));
}

export function dictationTooltip(
  state: DictationButtonState,
  progress: DictationProgress | null,
  shortcut: string,
  download: DictationDownload | null = null,
): string {
  switch (state) {
    case "load-needed":
      if (download === null) return `Set up dictation: load the speech model (${shortcut})`;
      return download.totalBytes === null || download.totalBytes <= 0
        ? `Set up dictation: one-time download (${shortcut})`
        : `Set up dictation: one-time ${formatFileSize(download.totalBytes)} download (${shortcut})`;
    case "loading":
      return progress === null || progress.totalBytes <= 0
        ? "Downloading speech model"
        : `Downloading speech model, ${dictationProgressPercent(progress)}% of ${formatFileSize(progress.totalBytes)}`;
    case "ready":
      return `Dictate (${shortcut})`;
    case "starting":
      return "Starting dictation";
    case "listening":
      return `Stop dictation (${shortcut})`;
    case "stopping":
      return "Finishing your words";
  }
}

const ARIA_LABELS: Record<DictationButtonState, string> = {
  "load-needed": "Set up dictation",
  loading: "Downloading the speech model",
  ready: "Dictate",
  starting: "Starting dictation",
  listening: "Stop dictation",
  stopping: "Finishing your words",
};

export interface DictationButtonAttrs {
  state: DictationButtonState;
  /** The download's progress, for ``loading``. */
  progress: DictationProgress | null;
  /** The download the model's first load is, when the probe says its files are not on this device; null when
   *  they are, and until that is known. */
  download: DictationDownload | null;
  shortcutLabel: string;
  disabled: boolean;
  onPress(): void;
  /** The label was pressed while the model's load is shown. */
  onDismissLoading(): void;
  /** Handed the setter that fills the capsule to a level from 0 to 1 while the glyph is in the document, and
   *  null when it leaves. */
  onMeter(setLevel: ((level: number) => void) | null): void;
}

// The capsule runs from y=2 to y=15 on the glyph's 24-unit grid; the fill is a rect clipped to it, grown upward.
const CAPSULE_BOTTOM = 15;
const CAPSULE_HEIGHT = 13;
const RING_RADIUS = 12.5;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

// The morph: a grid of one column holding the label, 0fr at rest and 1fr while loading, so the wrapper grows from
// the mic's 28px to the label's own width with no measuring; the mic sits over its right end and fades.
const MORPH_CLASS =
  "dictation-morph relative inline-grid h-[28px] min-w-[28px] shrink-0 transition-[grid-template-columns] " +
  "duration-[250ms] ease-[ease] motion-reduce:transition-none";
// A button with none of a button's chrome. No padding or border on the label itself: a 0fr column still holds
// its item's own, and that strip would show the first letter beside the mic at rest, so the padding sits on the
// text's own box inside the clipped label.
const LABEL_CLASS =
  "dictation-morph-label flex h-[28px] min-w-0 cursor-pointer appearance-none flex-col justify-center " +
  "overflow-hidden rounded-md border-0 bg-transparent p-0 text-left font-mono text-(length:--font-size-helper) " +
  "leading-[1.25] tracking-[0.02em] text-secondary select-none whitespace-nowrap focus-visible:outline-2 " +
  "focus-visible:outline-offset-2 focus-visible:outline-accent";
// The fade is on a span of its own rather than on the button, whose recipe carries a transition utility already;
// two on one element resolve by their order in the compiled bundle.
const MIC_CLASS =
  "dictation-morph-mic absolute top-0 right-0 transition-[opacity,scale] duration-[250ms] ease-[ease] " +
  "motion-reduce:transition-none";
const MIC_HIDDEN_CLASS = "pointer-events-none scale-[0.6] opacity-0";
// Centred on the 16px glyph's bottom-right corner inside the 28px button (the offsets count from inside the
// button's 1px border), ringed in the composer's background so it separates from the glyph.
const BADGE_CLASS =
  "dictation-setup-badge pointer-events-none absolute top-[16px] left-[16px] flex h-2.5 w-2.5 rounded-full " +
  "bg-composer text-warning shadow-[0_0_0_1.5px_var(--color-composer)]";

let instanceCounter = 0;

export function DictationButton(): m.Component<DictationButtonAttrs> {
  // A clip path is found by id across the whole document, so each button's is its own.
  instanceCounter += 1;
  const clipId = `dictation-capsule-${instanceCounter}`;
  let levelRect: SVGRectElement | null = null;
  // The state the last draw showed, which tells the update that enters ``loading`` from the ones after it.
  let drawnState: DictationButtonState | null = null;

  const setLevel = (level: number): void => {
    if (levelRect === null) return;
    const height = CAPSULE_HEIGHT * Math.max(0, Math.min(1, level));
    levelRect.setAttribute("y", (CAPSULE_BOTTOM - height).toFixed(2));
    levelRect.setAttribute("height", height.toFixed(2));
  };

  const glyph = (attrs: DictationButtonAttrs, isFaint: boolean): m.Vnode =>
    m(
      "svg",
      {
        class: isFaint ? "dictation-glyph text-faint" : "dictation-glyph",
        width: 16,
        height: 16,
        viewBox: "0 0 24 24",
        fill: "none",
        stroke: "currentColor",
        "stroke-width": 1.5,
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
        "aria-hidden": "true",
      },
      [
        m(
          "defs",
          m(
            "clipPath",
            { id: clipId },
            m("rect", {
              class: "dictation-level",
              x: 0,
              y: CAPSULE_BOTTOM,
              width: 24,
              height: 0,
              oncreate: (rectVnode: m.VnodeDOM) => {
                levelRect = rectVnode.dom as SVGRectElement;
                attrs.onMeter(setLevel);
              },
              onremove: () => {
                levelRect = null;
                attrs.onMeter(null);
              },
            }),
          ),
        ),
        m("path", {
          class: "dictation-capsule-fill",
          d: MIC_CAPSULE_PATH,
          fill: "currentColor",
          stroke: "none",
          "clip-path": `url(#${clipId})`,
        }),
        m.trust(iconPaths("mic")),
      ],
    );

  const ring = (progress: DictationProgress | null): m.Vnode => {
    const arc = (RING_CIRCUMFERENCE * dictationProgressPercent(progress)) / 100;
    return m(
      "svg",
      {
        class: "dictation-ring pointer-events-none absolute -inset-px",
        width: 28,
        height: 28,
        viewBox: "0 0 28 28",
        "aria-hidden": "true",
      },
      [
        m("circle", { class: "stroke-default", cx: 14, cy: 14, r: RING_RADIUS, fill: "none", "stroke-width": 2 }),
        m("circle", {
          class: "dictation-ring-arc stroke-accent",
          cx: 14,
          cy: 14,
          r: RING_RADIUS,
          fill: "none",
          "stroke-width": 2,
          "stroke-linecap": "round",
          "stroke-dasharray": `${arc.toFixed(2)} ${RING_CIRCUMFERENCE.toFixed(2)}`,
          transform: "rotate(-90 14 14)",
        }),
      ],
    );
  };

  const badge = (): m.Vnode => m("span", { class: BADGE_CLASS, "aria-hidden": "true" }, m.trust(alertDotIcon(10)));

  /** The label the mic eases into while the model's load is shown, which dismisses the load when pressed. At
   *  rest it is collapsed to nothing, and so kept out of reach of the pointer, the keyboard and a screen reader. */
  const label = (attrs: DictationButtonAttrs): m.Vnode => {
    const isLoading = attrs.state === "loading";
    const tooltip = isLoading
      ? `${dictationTooltip("loading", attrs.progress, attrs.shortcutLabel)}. Click to keep typing.`
      : null;
    return m(
      "button",
      {
        type: "button",
        class: LABEL_CLASS,
        disabled: !isLoading,
        tabindex: isLoading ? undefined : -1,
        "aria-hidden": isLoading ? undefined : "true",
        "aria-label": "Keep typing while the speech model loads",
        ...hoverTooltipAttrs(tooltip, "above"),
        onclick: () => attrs.onDismissLoading(),
      },
      m("span", { class: "flex flex-col px-1" }, [
        m("span", "Loading voice model"),
        m("span", {
          class: "dictation-morph-bar mt-0.5 h-0.5 bg-accent",
          style: { width: `${dictationProgressPercent(attrs.progress)}%` },
        }),
      ]),
    );
  };

  return {
    oncreate(vnode) {
      drawnState = vnode.attrs.state;
    },
    onupdate(vnode) {
      const isEnteringLoading = vnode.attrs.state === "loading" && drawnState !== "loading";
      drawnState = vnode.attrs.state;
      if (!isEnteringLoading) return;
      // The mic fades out under the label, so the focus it holds goes to the label: a press from the keyboard
      // leaves its focus ring on the control that is on screen, where Space and Enter are the label's own press.
      const mic = vnode.dom.querySelector(".message-input-dictate-button");
      if (mic === null || document.activeElement !== mic) return;
      vnode.dom.querySelector<HTMLElement>(".dictation-morph-label")?.focus({ preventScroll: true });
    },
    view(vnode) {
      const { state, progress, download, shortcutLabel, disabled, onPress } = vnode.attrs;
      const isBusy = state === "starting" || state === "stopping";
      const isLoading = state === "loading";
      const isLoadNeeded = state === "load-needed";
      const tooltip = dictationTooltip(state, progress, shortcutLabel, download);
      return m(
        "span",
        {
          class: `${MORPH_CLASS} ${isLoading ? "grid-cols-[1fr]" : "grid-cols-[0fr]"}`,
          "data-dictation-loading": isLoading ? "" : undefined,
        },
        [
          label(vnode.attrs),
          m(
            "span",
            { class: isLoading ? `${MIC_CLASS} ${MIC_HIDDEN_CLASS}` : MIC_CLASS },
            m(
              Button,
              {
                variant: "ghost",
                icon: true,
                sm: true,
                pressed: state === "listening",
                // A spinner is not a press, and while the load is shown the label over the mic takes the
                // pointer's: the button waits, explained by its tooltip.
                readonly: isLoading || isBusy,
                // Faded out, it leaves the tab order to the label as well.
                tabindex: isLoading ? -1 : undefined,
                disabled,
                extra: "message-input-dictate-button relative shrink-0 overflow-visible",
                "aria-label": ARIA_LABELS[state],
                "aria-pressed": state === "listening" ? "true" : "false",
                "data-dictation-state": state,
                ...hoverTooltipAttrs(tooltip, "above"),
                onclick: () => onPress(),
              },
              [
                isBusy ? m.trust(spinnerIcon(16)) : glyph(vnode.attrs, isLoadNeeded),
                isLoading ? ring(progress) : null,
                isLoadNeeded ? badge() : null,
              ],
            ),
          ),
        ],
      );
    },
  };
}

export interface DictationNoticeActions {
  /** Try again: the held words, or dictation itself. */
  onRetry(): void;
  /** Drop the held words. */
  onDiscard(): void;
  onDismiss(): void;
}

/** What dictation has to say, above the composer, with the ways out: Retry where it can help, Discard for held
 *  words, and Dismiss otherwise. */
export function dictationNotice(problem: DictationProblem, actions: DictationNoticeActions): m.Vnode {
  const buttons: m.Children[] = [];
  if (problem.canRetry) {
    buttons.push(
      m(
        Button,
        { variant: "primary", sm: true, extra: "message-input-dictation-retry shrink-0", onclick: actions.onRetry },
        "Retry",
      ),
    );
  }
  if (problem.kind === "held") {
    buttons.push(
      m(
        Button,
        { variant: "ghost", sm: true, extra: "message-input-dictation-discard shrink-0", onclick: actions.onDiscard },
        "Discard",
      ),
    );
  } else {
    buttons.push(
      m(
        Button,
        {
          variant: problem.canRetry ? "ghost" : "secondary",
          sm: true,
          extra: "message-input-dictation-dismiss shrink-0",
          onclick: actions.onDismiss,
        },
        "Dismiss",
      ),
    );
  }
  return m(
    "div",
    {
      class:
        "message-input-dictation-notice mb-2 flex flex-row items-center justify-between gap-3 rounded-[13px] border " +
        "border-default bg-surface-secondary py-3 pr-3 pl-5 text-(length:--font-size-body) text-secondary",
      role: "alert",
      "data-dictation-problem": problem.kind,
    },
    [m("span", problem.message), m("span", { class: "flex shrink-0 items-center gap-1" }, buttons)],
  );
}
