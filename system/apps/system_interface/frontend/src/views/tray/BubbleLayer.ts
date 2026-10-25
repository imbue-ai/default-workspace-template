/**
 * The speech bubbles (the multiplayer spec, section 6.5): 14px-rounded cards hanging above the tray with an arrow
 * down at the speaker's entry, each holding what was said, a dismiss X, and a click that opens the chat it was said
 * in. Bubbles of different speakers stack upward in arrival order. A mention of the viewer wears an accent border.
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { Bubble, BubbleStack } from "../../model/Bubbles";
import { foldedLabel } from "../../model/Bubbles";
import type { RosterParticipant } from "../../model/records";

const DISMISS_GLYPH_SIZE = 12;
/** How far a bubble keeps from the viewport's right edge, and where it hangs when its speaker has no entry. */
const BUBBLE_EDGE_INSET_PX = 16;
/** The least a bubble keeps from the viewport's right edge when its entry is near it. */
const BUBBLE_MIN_EDGE_INSET_PX = 8;
/** How far the bubble's right edge sits past the entry's, so the arrow (inset from the bubble's right) points at
 *  the entry. */
const BUBBLE_ARROW_OVERHANG_PX = 4;
/** The gap between a bubble and the top of the entry it hangs over. */
const BUBBLE_GAP_PX = 10;
/** How much higher each further bubble of the stack hangs. */
const BUBBLE_STACK_STEP_PX = 8;
/** Where a bubble hangs when its speaker has no entry: clear of the taskbar. */
const BUBBLE_FALLBACK_BOTTOM_PX = 56;

export interface BubbleLayerAttrs {
  readonly bubbles: BubbleStack;
  /** The right edge of each speaker's entry, in viewport px, where the bubble's arrow points; absent entries
   *  (a speaker whose entry is not drawn) hang the bubble at the tray's end. */
  readonly anchorOf: (speaker: string) => DOMRect | null;
  readonly rosterEntry: (key: string) => RosterParticipant | null;
  readonly onOpen: (speaker: string) => void;
  readonly onDismiss: (speaker: string) => void;
}

function speakerName(bubble: Bubble, entry: RosterParticipant | null): string {
  return entry?.display_name ?? bubble.speaker;
}

export const BubbleLayer: m.Component<BubbleLayerAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    if (attrs.bubbles.length === 0) return null;
    return m(
      "div",
      { "data-bubble-layer": "", class: "bubble-layer pointer-events-none fixed inset-0 z-(--z-sticky)" },
      attrs.bubbles.map((bubble, index) => {
        const anchor = attrs.anchorOf(bubble.speaker);
        const right =
          anchor === null
            ? BUBBLE_EDGE_INSET_PX
            : Math.max(BUBBLE_MIN_EDGE_INSET_PX, window.innerWidth - anchor.right - BUBBLE_ARROW_OVERHANG_PX);
        const bottom =
          (anchor === null ? BUBBLE_FALLBACK_BOTTOM_PX : Math.max(0, window.innerHeight - anchor.top)) +
          BUBBLE_GAP_PX +
          index * BUBBLE_STACK_STEP_PX;
        const entry = attrs.rosterEntry(bubble.speaker);
        return m(
          "div",
          {
            key: bubble.speaker,
            "data-bubble": bubble.speaker,
            "data-bubble-mention": bubble.isMention ? "true" : null,
            class:
              "bubble pointer-events-auto absolute flex max-w-(--desk-bubble-width) cursor-pointer items-start gap-1 rounded-(--desk-bubble-radius) " +
              "border bg-surface px-3 py-2 text-(length:--font-size-row) text-primary shadow-overlay " +
              (bubble.isMention ? "border-accent border-2 " : "border-default "),
            style: { right: `${right}px`, bottom: `${bottom}px`, zIndex: String(100 + index) },
            onclick: () => attrs.onOpen(bubble.speaker),
          },
          [
            m("div", { class: "flex min-w-0 flex-1 flex-col" }, [
              m(
                "div",
                { class: "truncate text-(length:--font-size-helper) text-faint" },
                `${speakerName(bubble, entry)} in ${bubble.chatTitle}`,
              ),
              bubble.lines.map((line, lineIndex) =>
                m("div", { key: lineIndex, class: "whitespace-pre-wrap break-words", "data-bubble-line": "" }, line),
              ),
              bubble.foldedCount > 0
                ? m("div", { class: "text-(length:--font-size-helper) text-faint" }, foldedLabel(bubble.foldedCount))
                : null,
            ]),
            m(
              "button",
              {
                type: "button",
                "data-bubble-dismiss": "",
                class:
                  "flex shrink-0 cursor-pointer items-center rounded border-0 bg-transparent p-0.5 text-faint hover:bg-fill-hover hover:text-primary",
                "aria-label": "Dismiss",
                onclick: (event: MouseEvent) => {
                  event.stopPropagation();
                  attrs.onDismiss(bubble.speaker);
                },
              },
              m.trust(icon("close", { size: DISMISS_GLYPH_SIZE })),
            ),
            m("span", {
              class:
                "bubble-arrow pointer-events-none absolute -bottom-1.5 right-4 size-3 rotate-45 border-r border-b bg-surface " +
                (bubble.isMention ? "border-accent" : "border-default"),
            }),
          ],
        );
      }),
    );
  },
};
