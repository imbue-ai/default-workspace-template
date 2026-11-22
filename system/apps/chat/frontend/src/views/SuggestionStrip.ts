/**
 * A strip: the suggestions of one placement, above the composer. Its suggestions are pills in a list on the user's
 * side of the column, one to a row and all as wide as the widest (the recommended one in green); their look is
 * ``style.css``'s (``suggestion-pill``).
 *
 * An activity or a new-chat strip is, open, its pills over a quiet link that switches the placement off, and,
 * collapsed, a small labelled toggle that opens it again. The next-step strip is one quiet line that is always
 * there: it says how many suggestions wait and shows them above itself, then offers to hide them again, with the
 * link that switches the placement off at its other end. The same line stands in the transcript under a reply that
 * has been answered (``views/AnsweredReplySuggestions.ts``).
 *
 * The strips of a chat share one column at the composer's width (``views/ChatSuggestions.ts``): an open strip, the
 * next-step line, and the line that follows a switch-off each take a row of their own under the toggles, which sit
 * side by side.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { Suggestion, SuggestionPlacement } from "../models/Suggestions";

/** What a placement's strip says of itself. */
interface PlacementWords {
  // What its suggestions are called: the name of their group, and what an activity or a new-chat strip's toggle says.
  label: string;
  disableLink: string;
  offNotice: string;
}

export const SUGGESTION_PLACEMENT_WORDS: Record<SuggestionPlacement, PlacementWords> = {
  next_step: {
    label: "Suggestions",
    disableLink: "disable next step suggestions",
    offNotice: "Next step suggestions are off.",
  },
  activity: {
    label: "While you wait",
    disableLink: "disable activity suggestions",
    offNotice: "Activity suggestions are off.",
  },
  new_chat: {
    label: "Things to try",
    disableLink: "disable new chat suggestions",
    offNotice: "New chat suggestions are off.",
  },
};

export interface SuggestionStripAttrs {
  placement: SuggestionPlacement;
  suggestions: readonly Suggestion[];
  isOpen: boolean;
  onPick: (suggestion: Suggestion) => void;
  onToggle: () => void;
  /** Switches the placement off; null for a viewer who is not offered that (anyone but the workspace's owner). */
  onDisable: (() => void) | null;
}

// A row of its own, under the toggles of the strips that are collapsed.
const OWN_ROW_CLASS = "order-last w-full";

// The user's side of the column: the group as wide as its widest pill and every pill stretched to that, set in from
// the column's edge as far as a person's bubble sets its text in.
const PILLS_CLASS = "suggestion-strip-pills ml-auto flex w-fit max-w-full flex-col items-stretch gap-1.5 pr-4";

const QUIET_LINK_CLASS =
  "cursor-pointer text-(length:--font-size-helper) text-faint hover:text-secondary hover:underline " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

// One height whatever it holds, so a set that arrives moves nothing.
const LINE_CLASS = "suggestion-line flex h-5 w-full items-center gap-3";

/** The suggestions as pills, in order; a click picks that one. */
export function suggestionPills(
  suggestions: readonly Suggestion[],
  onPick: (suggestion: Suggestion) => void,
): m.Vnode {
  return m(
    "div",
    { class: PILLS_CLASS },
    suggestions.map((suggestion) =>
      m(
        "button",
        {
          key: suggestion.suggestion_id,
          type: "button",
          class: suggestion.is_recommended ? "suggestion-pill suggestion-pill--recommended" : "suggestion-pill",
          onclick: () => onPick(suggestion),
        },
        suggestion.label,
      ),
    ),
  );
}

function disableLink(placement: SuggestionPlacement, onDisable: () => void): m.Vnode {
  return m(
    "button",
    { type: "button", class: `suggestion-strip-disable ${QUIET_LINK_CLASS}`, onclick: onDisable },
    SUGGESTION_PLACEMENT_WORDS[placement].disableLink,
  );
}

/** What the line of a reply's suggestions holds. */
export interface SuggestionLineAttrs {
  count: number;
  /** The suggestions are shown, so the line offers to hide them. */
  isOpen: boolean;
  onToggle: () => void;
  /** Switches next-step suggestions off, offered at the line's left end while the suggestions are shown; null
   *  where the line offers no such thing. */
  onDisable: (() => void) | null;
}

/**
 * The quiet line a reply's suggestions wait behind: how many there are, as the button that shows them, or the
 * button that hides them again. ``null`` draws the line empty, at its height.
 */
export function suggestionLine(attrs: SuggestionLineAttrs | null): m.Vnode {
  if (attrs === null) return m("div", { class: LINE_CLASS });
  const seeLabel = attrs.count === 1 ? "See 1 suggestion" : `See ${attrs.count} suggestions`;
  return m("div", { class: LINE_CLASS }, [
    attrs.isOpen && attrs.onDisable !== null ? disableLink("next_step", attrs.onDisable) : null,
    m(
      "button",
      {
        type: "button",
        class: `suggestion-line-toggle ml-auto ${QUIET_LINK_CLASS}`,
        "aria-expanded": attrs.isOpen ? "true" : "false",
        onclick: attrs.onToggle,
      },
      attrs.isOpen ? "Hide suggestions" : seeLabel,
    ),
  ]);
}

/** The next-step line with no set to speak of, which keeps its height above the composer. */
export function emptyNextStepLine(): m.Vnode {
  return m("div", { class: `suggestion-strip-placeholder ${OWN_ROW_CLASS}` }, suggestionLine(null));
}

export function SuggestionStrip(): m.Component<SuggestionStripAttrs> {
  return {
    view({ attrs }) {
      const words = SUGGESTION_PLACEMENT_WORDS[attrs.placement];
      if (attrs.placement === "next_step") {
        return m(
          "div",
          {
            class:
              `suggestion-strip ${attrs.isOpen ? "suggestion-strip--open" : "suggestion-strip--collapsed"} ` +
              `${OWN_ROW_CLASS} flex flex-col gap-1.5`,
            "data-placement": attrs.placement,
            role: "group",
            "aria-label": words.label,
          },
          [
            // The pills alone rise in: the line under them stays where it is.
            attrs.isOpen
              ? m("div", { class: "suggestion-rise" }, suggestionPills(attrs.suggestions, attrs.onPick))
              : null,
            suggestionLine({
              count: attrs.suggestions.length,
              isOpen: attrs.isOpen,
              onToggle: attrs.onToggle,
              onDisable: attrs.onDisable,
            }),
          ],
        );
      }
      if (!attrs.isOpen) {
        return m(
          "div",
          { class: "suggestion-strip suggestion-strip--collapsed", "data-placement": attrs.placement },
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
            [m("span", words.label), m.trust(icon("chevron-right", { size: 12, strokeWidth: 2 }))],
          ),
        );
      }
      return m(
        "div",
        {
          class: `suggestion-strip suggestion-strip--open suggestion-rise ${OWN_ROW_CLASS} flex flex-col gap-1`,
          "data-placement": attrs.placement,
          role: "group",
          "aria-label": words.label,
        },
        [
          suggestionPills(attrs.suggestions, attrs.onPick),
          attrs.onDisable === null
            ? null
            : m("div", { class: "flex justify-end" }, disableLink(attrs.placement, attrs.onDisable)),
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
      class: `suggestions-off-notice ${OWN_ROW_CLASS} flex items-center gap-1 text-(length:--font-size-helper) text-secondary`,
      "data-placement": placement,
      role: "status",
    },
    [
      m("span", SUGGESTION_PLACEMENT_WORDS[placement].offNotice),
      m(Button, { variant: "ghost", sm: true, extra: "suggestions-off-undo", onclick: onUndo }, "Undo"),
    ],
  );
}
