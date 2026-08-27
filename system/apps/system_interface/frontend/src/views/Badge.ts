import { TEXT_HELPER_SIZE } from "./typography";

/* ── Badge ───────────────────────────────────────────────────────────────────
 * One small-label chip, sized off the helper type token. Neutral/accent are
 * outline chips on a surface fill; the status tones carry a light tinted
 * fill. mono for id-like labels (model id, agent type).
 *
 * A class builder, not a component, on purpose: a census found two call
 * sites, both static <span>s with no states or behavior -- a wrapper would be
 * pure ceremony. Revisit if badges multiply the way buttons did. Only the
 * neutral and accent tones are in use today; the status tones are the
 * documented palette for future status chips.
 *
 * The leading `badge` class is a bare marker (no styling): a hook for tests
 * and the inspector. The Tailwind scanner reads utility names from the
 * literals in this file: keep every utility name a contiguous literal. */

export type BadgeTone = "neutral" | "accent" | "danger" | "warning" | "success";

export interface BadgeOptions {
  mono?: boolean;
  extra?: string;
}

// Border colour comes from the tone, never the base -- same
// one-utility-per-property rule as the button builder in views/Button.ts.
const BADGE_BASE =
  "badge inline-flex items-center gap-1 px-2 py-0.5 " +
  `${TEXT_HELPER_SIZE} font-normal leading-[1.4] whitespace-nowrap ` +
  "border rounded-lg";

const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: "bg-surface text-secondary border-default",
  accent: "bg-surface text-accent border-accent",
  danger: "bg-danger-surface text-danger border-danger-border",
  warning: "bg-warning-surface text-warning border-transparent",
  success: "bg-success/14 text-success border-success/35",
};

export function badgeClass(tone: BadgeTone, options: BadgeOptions = {}): string {
  const { mono = false, extra = "" } = options;
  const parts = [BADGE_BASE, BADGE_TONES[tone]];
  if (mono) parts.push("font-mono");
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}
