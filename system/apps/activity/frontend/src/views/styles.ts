/**
 * The recipes the System Monitor views share: section colours, status chips, headings, the technical-details panel,
 * and the quiet button that opens and closes a detail. One place, so the two tabs read as one app.
 *
 * Every colour pair a view draws is listed in ``CONTRAST_PAIRS`` by its design-token names, and
 * ``styles.test.ts`` checks each against the token values in workspace_ui's base.css: text at WCAG AA's 4.5:1,
 * marks (bar segments, swatches, the closing line) at 3:1 against what they sit on. A class here that is not in
 * that list is a pair nobody checked.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import type { ItemKind } from "../models/summary";

export const SECTION_HEADING_CLASS = "type-section text-secondary";
export const DETAILS_CLASS =
  "activity-details overflow-x-auto rounded-md bg-surface-secondary p-3 font-mono type-helper whitespace-pre-wrap text-secondary";
export const CHEVRON_SIZE = 12;

/** Which top-level section an item belongs to, and the colour that ties its rows to its slice of the bar. */
export type Section = "CHATS" | "APPS" | "BACKGROUND";

export interface SectionStyle {
  readonly label: string;
  /** The fill of its bar segment, its heading swatch and its rows' bars. */
  readonly fill: string;
}

// Three hues (green, slate, grey), each at least 3:1 against the bar's track; neighbouring segments are told apart
// by hue, label and a gap in the surface colour between them, not by contrast with each other.
export const SECTION_STYLES: Record<Section, SectionStyle> = {
  CHATS: { label: "Chats", fill: "bg-accent" },
  APPS: { label: "Apps", fill: "bg-stop-hover" },
  BACKGROUND: { label: "Background", fill: "bg-secondary" },
};

export function sectionOf(kind: ItemKind): Section {
  switch (kind) {
    case "CHAT":
    case "HELPER_AGENT":
    case "AGENT":
      return "CHATS";
    case "APP":
      return "APPS";
    case "SERVICE":
    case "PLUMBING":
      return "BACKGROUND";
  }
}

export function sectionAnchorId(section: Section): string {
  return `section-${section.toLowerCase()}`;
}

export type ChipTone = "success" | "warning" | "danger";

// The shared badge recipe's light status text misses 4.5:1 on its tinted fills, so these keep the tint and carry
// the text in a colour that clears it.
const CHIP_BASE = "activity-chip inline-flex items-center gap-1 rounded-lg border px-2 py-0.5 type-helper";
const CHIP_TONES: Record<ChipTone, string> = {
  success: "bg-success-surface text-accent border-success-border",
  warning: "bg-warning-surface text-primary border-transparent",
  danger: "bg-danger-surface text-danger-hover border-danger-border",
};

export function chipClass(tone: ChipTone): string {
  return `${CHIP_BASE} ${CHIP_TONES[tone]}`;
}

export const CLOSING_LINE_FILL = "bg-danger-hover";
export const CLOSING_TEXT = "text-danger-hover";

export interface ContrastPair {
  readonly what: string;
  /** Token names as base.css declares them, without the ``--c-`` prefix. */
  readonly foreground: string;
  readonly background: string;
  readonly kind: "text" | "mark";
}

export const CONTRAST_PAIRS: readonly ContrastPair[] = [
  {
    what: "secondary text: headings, sizes, tips, axis labels",
    foreground: "text-secondary",
    background: "bg",
    kind: "text",
  },
  { what: "primary text", foreground: "text-primary", background: "bg", kind: "text" },
  { what: "error text", foreground: "danger-hover", background: "bg", kind: "text" },
  { what: "details panel text", foreground: "text-secondary", background: "surface-secondary", kind: "text" },
  { what: "comfortable chip", foreground: "accent", background: "success-bg", kind: "text" },
  { what: "getting-tight chip", foreground: "text-primary", background: "warning-bg", kind: "text" },
  { what: "almost-full and closed-first chips", foreground: "danger-hover", background: "danger-bg", kind: "text" },
  { what: "closing-line label", foreground: "danger-hover", background: "bg", kind: "text" },
  { what: "warning banner text", foreground: "text-primary", background: "warning-bg", kind: "text" },
  { what: "chats segment", foreground: "accent", background: "surface-secondary", kind: "mark" },
  { what: "apps segment", foreground: "stop-button-hover", background: "surface-secondary", kind: "mark" },
  { what: "background segment", foreground: "text-secondary", background: "surface-secondary", kind: "mark" },
  { what: "closing line", foreground: "danger-hover", background: "surface-secondary", kind: "mark" },
  { what: "chats row bars", foreground: "accent", background: "bg", kind: "mark" },
  { what: "apps row bars", foreground: "stop-button-hover", background: "bg", kind: "mark" },
  { what: "background row bars", foreground: "text-secondary", background: "bg", kind: "mark" },
  { what: "history line", foreground: "accent", background: "bg", kind: "mark" },
  { what: "history closing line and closure marks", foreground: "danger-hover", background: "bg", kind: "mark" },
  { what: "history tooltip", foreground: "text-primary", background: "surface", kind: "text" },
  { what: "history tooltip closure", foreground: "danger-hover", background: "surface", kind: "text" },
];

/** A quiet button that opens or closes a detail, reporting which it is to assistive tech. */
export function disclosure(isOpen: boolean, closedLabel: string, openLabel: string, onToggle: () => void): m.Vnode {
  return m(
    Button,
    {
      variant: "ghost",
      sm: true,
      quiet: true,
      extra: "activity-disclose -ml-3 self-start",
      "aria-expanded": String(isOpen),
      onclick: onToggle,
    },
    isOpen ? openLabel : closedLabel,
  );
}
