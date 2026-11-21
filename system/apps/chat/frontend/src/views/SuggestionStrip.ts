/**
 * A strip: the suggestions of one placement, above the composer and at its width. Open, it is its suggestions as
 * pills in a wrapping row (the recommended one in green) over a quiet link that switches the placement off;
 * collapsed, it is a small labelled toggle that opens it again. A set that may run long shows its first few with
 * a "Show more" that reveals the rest. The pills' look is ``style.css``'s (``suggestion-pill``).
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { Suggestion, SuggestionPlacement } from "../models/Suggestions";

/** What a placement's strip says of itself. */
interface PlacementWords {
  toggleLabel: string;
  disableLink: string;
  offNotice: string;
}

export const SUGGESTION_PLACEMENT_WORDS: Record<SuggestionPlacement, PlacementWords> = {
  next_step: {
    toggleLabel: "Next steps",
    disableLink: "disable next step suggestions",
    offNotice: "Next step suggestions are off.",
  },
  activity: {
    toggleLabel: "While you wait",
    disableLink: "disable activity suggestions",
    offNotice: "Activity suggestions are off.",
  },
  new_chat: {
    toggleLabel: "Things to try",
    disableLink: "disable new chat suggestions",
    offNotice: "New chat suggestions are off.",
  },
};

export interface SuggestionStripAttrs {
  placement: SuggestionPlacement;
  suggestions: readonly Suggestion[];
  /** How many suggestions are drawn before "Show more" is clicked; null draws every one. */
  foldAfter: number | null;
  isOpen: boolean;
  onPick: (suggestion: Suggestion) => void;
  onToggle: () => void;
  /** Switches the placement off; null for a viewer who is not offered that (anyone but the workspace's owner). */
  onDisable: (() => void) | null;
}

// The composer's own column, so the pills line up with its edge.
const COLUMN_CLASS = "mx-auto mb-2 w-full max-w-(--width-message-column)";
const STRIP_CLASS = `suggestion-strip ${COLUMN_CLASS}`;

// A key no suggestion's own id takes, for the control drawn among the keyed pills.
const SHOW_MORE_KEY = "suggestion-strip-show-more";

export function SuggestionStrip(): m.Component<SuggestionStripAttrs> {
  // Kept through a collapse to the toggle and back, so the rest stay revealed once asked for.
  let isUnfolded = false;

  return {
    view({ attrs }) {
      const words = SUGGESTION_PLACEMENT_WORDS[attrs.placement];
      const shown =
        attrs.foldAfter === null || isUnfolded ? attrs.suggestions : attrs.suggestions.slice(0, attrs.foldAfter);
      const isMoreToShow = shown.length < attrs.suggestions.length;
      if (!attrs.isOpen) {
        return m(
          "div",
          { class: `${STRIP_CLASS} suggestion-strip--collapsed`, "data-placement": attrs.placement },
          m(
            Button,
            {
              variant: "ghost",
              sm: true,
              quiet: true,
              extra: "suggestion-strip-toggle",
              "aria-expanded": "false",
              onclick: attrs.onToggle,
            },
            [m("span", words.toggleLabel), m.trust(icon("chevron-right", { size: 12, strokeWidth: 2 }))],
          ),
        );
      }
      return m(
        "div",
        {
          class: `${STRIP_CLASS} suggestion-strip--open flex flex-col gap-1`,
          "data-placement": attrs.placement,
          role: "group",
          "aria-label": words.toggleLabel,
        },
        [
          m(
            "div",
            { class: "suggestion-strip-pills flex flex-wrap items-center gap-1.5" },
            [
              ...shown.map((suggestion) =>
                m(
                  "button",
                  {
                    key: suggestion.suggestion_id,
                    type: "button",
                    class: suggestion.is_recommended
                      ? "suggestion-pill suggestion-pill--recommended"
                      : "suggestion-pill",
                    onclick: () => attrs.onPick(suggestion),
                  },
                  suggestion.label,
                ),
              ),
              isMoreToShow
                ? m(
                    Button,
                    {
                      key: SHOW_MORE_KEY,
                      variant: "ghost",
                      sm: true,
                      quiet: true,
                      extra: "suggestion-strip-more",
                      onclick: () => {
                        isUnfolded = true;
                      },
                    },
                    "Show more",
                  )
                : null,
            ].filter((child) => child !== null),
          ),
          attrs.onDisable === null
            ? null
            : m(
                "div",
                { class: "flex justify-end" },
                m(
                  "button",
                  {
                    type: "button",
                    class:
                      "suggestion-strip-disable cursor-pointer text-(length:--font-size-helper) text-faint " +
                      "hover:text-secondary hover:underline focus-visible:outline-2 " +
                      "focus-visible:outline-offset-2 focus-visible:outline-accent",
                    onclick: attrs.onDisable,
                  },
                  words.disableLink,
                ),
              ),
        ],
      );
    },
  };
}

/** What takes a strip's place for a few seconds once its link switched the placement off: one line, and a way back. */
export function suggestionsOffNotice(placement: SuggestionPlacement, onUndo: () => void): m.Vnode {
  return m(
    "div",
    {
      class: `suggestions-off-notice ${COLUMN_CLASS} flex items-center gap-1 text-(length:--font-size-helper) text-secondary`,
      "data-placement": placement,
      role: "status",
    },
    [
      m("span", SUGGESTION_PLACEMENT_WORDS[placement].offNotice),
      m(Button, { variant: "ghost", sm: true, extra: "suggestions-off-undo", onclick: onUndo }, "Undo"),
    ],
  );
}
