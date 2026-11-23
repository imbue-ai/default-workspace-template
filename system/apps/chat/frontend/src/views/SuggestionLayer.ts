/**
 * The layer the pills of the strip above the composer float in while they are shown: over the end of the
 * transcript, directly above the footer that holds the strip's line (``views/ChatSuggestions.ts``).
 *
 * It is the last thing in the transcript's scroll element and sticks to that element's bottom edge. So the
 * transcript keeps its full height and scrolls underneath, the layer's own height is the room at the transcript's
 * end that lets the last reply be scrolled clear of the pills, and a wheel or a finger on a pill scrolls the
 * transcript. The scroll engine is told which element it is (``views/transcript-scroll-engine.ts``).
 *
 * The layer takes no clicks, only its pills do: the space beside and between them belongs to the transcript
 * underneath (``style.css``'s ``suggestion-layer``). A list taller than the layer's share of the transcript's
 * height scrolls inside it. The layer is always drawn, with nothing in it and no height while no pills are shown.
 */

import m from "mithril";
import { openStrip } from "./ChatSuggestions";
import { SUGGESTION_PLACEMENT_WORDS, suggestionPills } from "./SuggestionStrip";

export interface SuggestionLayerAttrs {
  chatId: string;
  /** Told the layer's element once it is drawn, and null as it goes. */
  onElement?: (element: HTMLElement | null) => void;
}

// Pushed to the scroll element's end and stuck to its bottom edge, at most 60% of its height.
const LAYER_CLASS = "suggestion-layer sticky bottom-0 z-(--z-sticky) mt-auto flex max-h-[60%] w-full flex-col";

// The transcript's own column, so the pills line up with a person's bubbles.
const GROUP_CLASS = "suggestion-layer-strip mx-auto flex min-h-0 w-full max-w-(--width-message-column) flex-col";

// The pills alone rise in. The padding keeps a focused pill's outline, and the last one's shadow, inside what
// scrolls, and sets the pills off from the line under the layer.
const SCROLL_CLASS = "suggestion-layer-scroll suggestion-rise min-h-0 overflow-y-auto pt-1 pb-1.5 pl-1";

export function SuggestionLayer(): m.Component<SuggestionLayerAttrs> {
  return {
    oncreate(vnode) {
      vnode.attrs.onElement?.(vnode.dom as HTMLElement);
    },

    onremove(vnode) {
      vnode.attrs.onElement?.(null);
    },

    view({ attrs }) {
      const strip = openStrip(attrs.chatId);
      return m(
        "div",
        { class: LAYER_CLASS },
        strip === null
          ? []
          : [
              m(
                "div",
                {
                  // Another set's pills rise in rather than changing in place.
                  key: `${strip.placement}:${strip.setKey}`,
                  class: GROUP_CLASS,
                  "data-placement": strip.placement,
                  role: "group",
                  "aria-label": SUGGESTION_PLACEMENT_WORDS[strip.placement].label,
                },
                m("div", { class: SCROLL_CLASS }, suggestionPills(strip.suggestions, strip.onPick)),
              ),
            ],
      );
    },
  };
}
