/**
 * The workspace's segmented control: a few choices side by side in one outlined well, exactly one of them chosen and
 * filled. A segment shows a glyph (named by its label, which the tooltip and a screen reader read) or its label.
 *
 * It is a radio group: the chosen segment is the one tab stop, and the arrow keys move the choice along it.
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility name a contiguous
 * literal.
 */

import m from "mithril";

import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon, type IconName } from "./icons";

export interface Segment<V extends string> {
  value: V;
  label: string;
  /** Show this glyph in place of the label, which becomes the segment's tooltip and accessible name. */
  icon?: IconName;
  /** More attributes for the segment's button (a `data-*` hook for tests). */
  attrs?: m.Attributes;
}

export interface SegmentedControlAttrs<V extends string> {
  /** What the group chooses, for a screen reader. */
  label: string;
  segments: readonly Segment<V>[];
  value: V;
  onSelect: (value: V) => void;
}

const WELL_CLASS = "segmented flex shrink-0 items-center gap-0.5 rounded-lg border border-default p-0.5";
const SEGMENT_CLASS =
  "flex h-6 cursor-pointer items-center justify-center rounded-md transition-colors duration-(--dur-base) " +
  "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent";
const GLYPH_SEGMENT_CLASS = "w-7";
const LABEL_SEGMENT_CLASS = "px-2.5 text-(length:--font-size-row)";
const CHOSEN_CLASS = "bg-fill-active text-primary";
const IDLE_CLASS = "text-secondary hover:bg-fill-hover hover:text-primary";

export function SegmentedControl<V extends string>(): m.Component<SegmentedControlAttrs<V>> {
  return {
    view({ attrs: { label, segments, value, onSelect } }) {
      const onKeydown = (event: KeyboardEvent): void => {
        const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[event.key];
        if (step === undefined) return;
        event.preventDefault();
        const index = segments.findIndex((segment) => segment.value === value);
        const next = segments[(index + step + segments.length) % segments.length];
        onSelect(next.value);
        const group = (event.currentTarget as HTMLElement).closest('[role="radiogroup"]');
        // The chosen segment is the tab stop, so focus follows the choice once the redraw has moved it.
        requestAnimationFrame(() =>
          group?.querySelector<HTMLElement>(`[data-segment="${CSS.escape(next.value)}"]`)?.focus(),
        );
      };
      return m(
        "div",
        { role: "radiogroup", "aria-label": label, class: WELL_CLASS },
        segments.map((segment) => {
          const isChosen = segment.value === value;
          return m(
            "button",
            {
              key: segment.value,
              type: "button",
              role: "radio",
              "aria-checked": isChosen ? "true" : "false",
              "data-segment": segment.value,
              tabindex: isChosen ? 0 : -1,
              class: `${SEGMENT_CLASS} ${segment.icon === undefined ? LABEL_SEGMENT_CLASS : GLYPH_SEGMENT_CLASS} ${
                isChosen ? CHOSEN_CLASS : IDLE_CLASS
              }`,
              ...(segment.icon === undefined
                ? {}
                : { "aria-label": segment.label, ...hoverTooltipAttrs(segment.label) }),
              ...segment.attrs,
              onclick: () => {
                if (!isChosen) onSelect(segment.value);
              },
              onkeydown: onKeydown,
            },
            segment.icon === undefined ? segment.label : m.trust(icon(segment.icon, { size: 15 })),
          );
        }),
      );
    },
  };
}
