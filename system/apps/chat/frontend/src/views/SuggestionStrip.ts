/**
 * A strip: the suggestions of one placement, above the composer. Its suggestions are pills in a list on the user's
 * side of the column, one to a row and all as wide as the widest (the recommended one in green); their look is
 * ``style.css``'s (``suggestion-pill``).
 *
 * Both placements read alike: the pills, and one quiet line under them. The two are drawn apart: the pills float
 * over the end of the transcript (``views/SuggestionLayer.ts``), and the line, which this module's component
 * draws, stays in the footer directly above the composer, so showing or hiding the pills moves nothing there. The
 * next-step strip's line is always there: it says how many suggestions wait and shows them, then offers to hide
 * them again, with the link that switches the placement off beside that. The same line stands in the transcript
 * under a reply that has been answered (``views/AnsweredReplySuggestions.ts``). A new-chat strip's pills are always
 * shown, and its line holds its link alone.
 *
 * The lines of a chat's strips share one column at the composer's width (``views/ChatSuggestions.ts``).
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import type { Suggestion, SuggestionPlacement } from "../models/Suggestions";

/** What a placement's strip says of itself. */
interface PlacementWords {
  // What its suggestions are called: the accessible name of their group.
  label: string;
  disableLink: string;
  offNotice: string;
}

export const SUGGESTION_PLACEMENT_WORDS: Record<SuggestionPlacement, PlacementWords> = {
  next_step: {
    label: "Suggestions",
    disableLink: "Disable suggestions",
    offNotice: "Next step suggestions are off.",
  },
  new_chat: {
    label: "Things to try",
    disableLink: "Disable new chat suggestions",
    offNotice: "New chat suggestions are off.",
  },
};

export interface SuggestionStripAttrs {
  placement: SuggestionPlacement;
  /** How many suggestions the strip holds. */
  count: number;
  /** The pills are shown, in the layer over the transcript's end. */
  isOpen: boolean;
  /** Shows the pills from the strip's line, or hides them behind it; null for a strip whose pills are always
   *  shown, whose line then holds its link alone. */
  onToggle: (() => void) | null;
  /** Switches the placement off; null for a viewer who is not offered that (anyone but the workspace's owner). */
  onDisable: (() => void) | null;
}

// The user's side of the column: the group as wide as its widest pill and every pill stretched to that, ending at
// the column's edge, where the composer's box ends.
const PILLS_CLASS = "suggestion-strip-pills ml-auto flex w-fit max-w-full flex-col items-stretch gap-1.5";

const QUIET_LINK_CLASS =
  "cursor-pointer text-(length:--font-size-helper) text-faint hover:text-secondary hover:underline " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

// One height whatever it holds, so a set that arrives moves nothing. What it holds sits at its right end.
const LINE_CLASS = "suggestion-line flex h-5 w-full items-center justify-end gap-4";

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

// The words on a link beside its partner in a reply's line, which say what they act on in their name and tooltip.
const DISABLE_SHORT = "Disable";
const HIDE_SHORT = "Hide";
const HIDE_LINK = "Hide suggestions";

/** The link that switches ``placement`` off: in full where it stands alone, or ``short`` beside Hide with the full
 *  words as its name and tooltip. */
function disableLink(placement: SuggestionPlacement, onDisable: () => void, isShort: boolean): m.Vnode {
  const words = SUGGESTION_PLACEMENT_WORDS[placement].disableLink;
  return m(
    "button",
    {
      type: "button",
      class: `suggestion-strip-disable ${QUIET_LINK_CLASS}`,
      onclick: onDisable,
      ...(isShort ? { "aria-label": words, ...hoverTooltipAttrs(words, "above") } : {}),
    },
    isShort ? DISABLE_SHORT : words,
  );
}

/** What the line of a reply's suggestions holds. */
export interface SuggestionLineAttrs {
  count: number;
  /** The suggestions are shown, so the line offers to hide them. */
  isOpen: boolean;
  onToggle: () => void;
  /** Switches next-step suggestions off, offered beside the button that hides the suggestions while they are
   *  shown; null where the line offers no such thing. */
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
    attrs.isOpen && attrs.onDisable !== null ? disableLink("next_step", attrs.onDisable, true) : null,
    m(
      "button",
      {
        type: "button",
        class: `suggestion-line-toggle ${QUIET_LINK_CLASS}`,
        "aria-expanded": attrs.isOpen ? "true" : "false",
        onclick: attrs.onToggle,
        ...(attrs.isOpen ? { "aria-label": HIDE_LINK, ...hoverTooltipAttrs(HIDE_LINK, "above") } : {}),
      },
      attrs.isOpen ? HIDE_SHORT : seeLabel,
    ),
  ]);
}

/** The next-step line with no set to speak of, which keeps its height above the composer. */
export function emptyNextStepLine(): m.Vnode {
  return m("div", { class: "suggestion-strip-placeholder" }, suggestionLine(null));
}

/** The line under a strip whose pills are always shown: its link alone, at the height of a reply's line. */
function linkLine(placement: SuggestionPlacement, onDisable: (() => void) | null): m.Vnode {
  return m("div", { class: LINE_CLASS }, onDisable === null ? null : disableLink(placement, onDisable, false));
}

/** A strip's part in the footer: its line. */
export function SuggestionStrip(): m.Component<SuggestionStripAttrs> {
  return {
    view({ attrs }) {
      return m(
        "div",
        {
          class: `suggestion-strip ${attrs.isOpen ? "suggestion-strip--open" : "suggestion-strip--collapsed"}`,
          "data-placement": attrs.placement,
        },
        attrs.onToggle === null
          ? linkLine(attrs.placement, attrs.onDisable)
          : suggestionLine({
              count: attrs.count,
              isOpen: attrs.isOpen,
              onToggle: attrs.onToggle,
              onDisable: attrs.onDisable,
            }),
      );
    },
  };
}

/** What takes a strip's place for a few seconds once its link switched the placement off: one line, and a way back. */
export function suggestionsOffNotice(placement: SuggestionPlacement, onUndo: () => void): m.Vnode {
  return m(
    "div",
    {
      class: "suggestions-off-notice flex items-center gap-1 text-(length:--font-size-helper) text-secondary",
      "data-placement": placement,
      role: "status",
    },
    [
      m("span", SUGGESTION_PLACEMENT_WORDS[placement].offNotice),
      m(Button, { variant: "ghost", sm: true, extra: "suggestions-off-undo", onclick: onUndo }, "Undo"),
    ],
  );
}
