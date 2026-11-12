/**
 * The composer's mic (the workspace-voice-input spec, D5): one button that carries every state of dictation, with
 * no bar and no timer. While listening its capsule fills from the bottom with the input level, drawn inside the
 * glyph so nothing in the toolbar moves; while the speech model downloads a ring around it shows the progress;
 * starting and stopping show a spinner. The level is set imperatively, through the setter the button hands its
 * owner, since it changes every frame and a redraw per frame would redraw the whole page.
 *
 * Beside it, the notice the composer shows over itself when dictation has something to say, in the style of its
 * provider notice.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { MIC_CAPSULE_PATH, iconPaths, spinnerIcon } from "@imbue/workspace-ui/src/components/icons";
import type { DictationProblem, DictationState } from "@imbue/workspace-voice/src/dictation";
import { formatFileSize } from "../models/attachments";

export interface DictationProgress {
  loadedBytes: number;
  totalBytes: number;
}

/** The whole-number percent of a download, for the tooltip and the ring. */
export function dictationProgressPercent(progress: DictationProgress | null): number {
  if (progress === null || progress.totalBytes <= 0) return 0;
  return Math.min(100, Math.floor((100 * progress.loadedBytes) / progress.totalBytes));
}

export function dictationTooltip(state: DictationState, progress: DictationProgress | null, shortcut: string): string {
  switch (state) {
    case "idle":
      return `Dictate (${shortcut})`;
    case "loading":
      return progress === null || progress.totalBytes <= 0
        ? "Downloading speech model"
        : `Downloading speech model, ${dictationProgressPercent(progress)}% of ${formatFileSize(progress.totalBytes)}`;
    case "starting":
      return "Starting dictation";
    case "listening":
      return `Stop dictation (${shortcut})`;
    case "stopping":
      return "Finishing your words";
  }
}

const ARIA_LABELS: Record<DictationState, string> = {
  idle: "Dictate",
  loading: "Downloading the speech model",
  starting: "Starting dictation",
  listening: "Stop dictation",
  stopping: "Finishing your words",
};

export interface DictationButtonAttrs {
  state: DictationState;
  progress: DictationProgress | null;
  shortcutLabel: string;
  disabled: boolean;
  onToggle(): void;
  /** Handed the setter that fills the capsule to a level from 0 to 1 while the glyph is in the document, and
   *  null when it leaves. */
  onMeter(setLevel: ((level: number) => void) | null): void;
}

// The capsule runs from y=2 to y=15 on the glyph's 24-unit grid; the fill is a rect clipped to it, grown upward.
const CAPSULE_BOTTOM = 15;
const CAPSULE_HEIGHT = 13;
const RING_RADIUS = 12.5;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

let instanceCounter = 0;

export function DictationButton(): m.Component<DictationButtonAttrs> {
  // A clip path is found by id across the whole document, so each button's is its own.
  instanceCounter += 1;
  const clipId = `dictation-capsule-${instanceCounter}`;
  let levelRect: SVGRectElement | null = null;

  const setLevel = (level: number): void => {
    if (levelRect === null) return;
    const height = CAPSULE_HEIGHT * Math.max(0, Math.min(1, level));
    levelRect.setAttribute("y", (CAPSULE_BOTTOM - height).toFixed(2));
    levelRect.setAttribute("height", height.toFixed(2));
  };

  const glyph = (attrs: DictationButtonAttrs): m.Vnode =>
    m(
      "svg",
      {
        class: "dictation-glyph",
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
          class: "dictation-ring-arc stroke-accent-surface",
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

  return {
    view(vnode) {
      const { state, progress, shortcutLabel, disabled, onToggle } = vnode.attrs;
      const isBusy = state === "starting" || state === "stopping";
      return m(
        Button,
        {
          variant: "ghost",
          icon: true,
          sm: true,
          pressed: state === "listening",
          // A download or a spinner is not a press: the button waits, explained by its tooltip.
          readonly: state === "loading" || isBusy,
          disabled,
          extra: "message-input-dictate-button relative shrink-0 overflow-visible",
          "aria-label": ARIA_LABELS[state],
          "aria-pressed": state === "listening" ? "true" : "false",
          "data-dictation-state": state,
          ...hoverTooltipAttrs(dictationTooltip(state, progress, shortcutLabel), "above"),
          onclick: () => onToggle(),
        },
        [isBusy ? m.trust(spinnerIcon(16)) : glyph(vnode.attrs), state === "loading" ? ring(progress) : null],
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
        "border-default bg-body-surface-secondary py-3 pr-3 pl-5 text-(length:--font-size-body) text-body-content-secondary",
      role: "alert",
      "data-dictation-problem": problem.kind,
    },
    [m("span", problem.message), m("span", { class: "flex shrink-0 items-center gap-1" }, buttons)],
  );
}
