// Shared class-string builders for the input/badge/modal primitives. The
// recipes live as Tailwind utility strings carried by the markup, not as
// .input/.badge CSS classes -- one source of truth in code; to restyle every
// input, edit this file. The button primitive lives in views/Button.ts (its
// recipe plus the Button component that carries it).
//
// Each builder leads with a bare marker class ("input", "badge",
// "modal-card", ...). The markers carry no styling of their own; they are
// stable hooks for tests, for the few contextual stylesheet rules that key off
// them (e.g. `.claude-login-subtle-body .input`), and for readability in the
// inspector.
//
// The Tailwind scanner reads the utility names from the literals in this file
// (`@source "./**/*.ts"` in style.css), so classes assembled here are always
// generated. Keep every utility name a contiguous literal -- never build one
// by string interpolation.

// Type + motion fragments shared by the recipes here and in views/Button.ts.
// Sizes reference the --font-size-* role tokens (see style.css) rather than
// Tailwind's text-* steps, so the type scale stays the single source of truth.
export const TEXT_BODY_SIZE = "text-(length:--font-size-body)";
const TEXT_HELPER_SIZE = "text-(length:--font-size-helper)";
const TEXT_HEADING_SIZE = "text-(length:--font-size-heading)";

/* ── Input ───────────────────────────────────────────────────────────────────
 * One text-field style for <input> / <textarea>. Options: mono (id/token-like
 * fields), withAction (reserves room for a trailing inline action button),
 * extra. Text fields are always focus-visible, so the accent ring also shows
 * on click. Genuinely special fields stay bespoke: the composer textbox, the
 * model-search field, and the inline tab-rename editor. */

export interface InputOptions {
  mono?: boolean;
  withAction?: boolean;
  extra?: string;
}

const INPUT_BASE =
  "input block w-full px-3 py-2 " +
  `${TEXT_BODY_SIZE} text-primary placeholder:text-faint ` +
  "bg-surface border border-default rounded-md " +
  "transition-[border-color,box-shadow] duration-(--dur-base) ease-[ease] " +
  "focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
  "disabled:opacity-50 disabled:cursor-not-allowed";

export function inputClass(options: InputOptions = {}): string {
  const { mono = false, withAction = false, extra = "" } = options;
  const parts = [INPUT_BASE];
  if (mono) parts.push("font-mono tracking-[0.01em]");
  if (withAction) parts.push("pr-16");
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}

/* ── Badge ───────────────────────────────────────────────────────────────────
 * One small-label chip, sized off the helper type token. Neutral/accent are
 * outline chips on a surface fill; the status tones carry a light tinted
 * fill. mono for id-like labels (model id, agent type). */

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

/* ── Modal shell ─────────────────────────────────────────────────────────────
 * The dimmed overlay + centered card + header/title + body copy + actions row
 * the workspace's dialogs share. Emitted by views/Modal.ts; the copy classes
 * (message, label) are used directly by callers for dialog body content. The
 * enter animations' @keyframes (modal-overlay-in / modal-card-in) live in
 * style.css. `.modal-card` also anchors contextual stylesheet rules (the
 * glyph-picker pressed-state feedback). */

export const MODAL_OVERLAY_CLASS =
  "modal-overlay fixed inset-0 z-(--z-overlay) flex items-center justify-center bg-black/40 " +
  "animate-[modal-overlay-in_150ms_ease-out]";

export const MODAL_CARD_CLASS =
  "modal-card w-[420px] max-w-[90vw] p-6 bg-surface border border-default rounded-lg shadow-overlay " +
  "animate-[modal-card-in_var(--dur-slow)_cubic-bezier(0.16,1,0.3,1)]";

export const MODAL_HEADER_CLASS = "modal-header mb-4 flex items-center gap-2";

export const MODAL_TITLE_CLASS = `modal-title m-0 ${TEXT_HEADING_SIZE} font-semibold text-primary`;

// Dialog body copy reads at full strength by default -- it is the point of the
// dialog. A genuinely secondary line (a footnote, a hint) opts into
// text-secondary / text-faint at its own call site.
export const MODAL_BODY_CLASS = "modal-body type-body text-primary";

export const MODAL_MESSAGE_CLASS = "modal-message type-body mb-4 text-primary";

export const MODAL_LABEL_CLASS = `modal-label mb-1 block ${TEXT_BODY_SIZE} font-medium text-secondary`;

export const MODAL_ACTIONS_CLASS = "modal-actions flex justify-end gap-2";
