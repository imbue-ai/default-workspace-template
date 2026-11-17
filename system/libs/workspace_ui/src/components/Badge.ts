import { TEXT_HELPER_SIZE } from "./typography";

/* Badge.
 * One small-label chip, sized off the helper type token. Neutral/accent are
 * outline chips on a surface fill; the status tones carry a light tinted
 * fill. mono for id-like labels (model id, agent type).
 *
 * A class builder, not a component, on purpose: badge call sites are static
 * <span>s with no states or behavior, so a wrapper would be pure ceremony.
 * Revisit if badges multiply the way buttons did.
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
// one-utility-per-property rule as the button builder in Button.ts.
const BADGE_BASE =
  "badge inline-flex items-center gap-1 px-2 py-0.5 " +
  `${TEXT_HELPER_SIZE} font-normal leading-[1.4] whitespace-nowrap ` +
  "border rounded-md";

// The brand's status pill: each tone's subtle tint, its text in the tone's subtle content (7:1 on the tint), and
// an edge one ramp step deeper than the tint.
const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: "bg-body-surface-raised text-body-content-secondary border-default",
  accent: "bg-accent-subtle-surface text-accent-subtle-content border-accent-4",
  danger: "bg-danger-subtle-surface text-danger-subtle-content border-danger-4",
  warning: "bg-warn-subtle-surface text-warn-subtle-content border-warn-4",
  success: "bg-success-subtle-surface text-success-subtle-content border-success-4",
};

export function badgeClass(tone: BadgeTone, options: BadgeOptions = {}): string {
  const { mono = false, extra = "" } = options;
  const parts = [BADGE_BASE, BADGE_TONES[tone]];
  if (mono) parts.push("font-mono");
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}
