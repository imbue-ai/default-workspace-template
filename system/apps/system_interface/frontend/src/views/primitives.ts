// Shared class-string builders for the component primitives (mirrors
// apps/minds' views/components/constants.ts). The recipes live HERE as Tailwind
// utility strings carried by the markup, not as .btn/.input/.badge CSS classes
// -- one source of truth in code; to restyle every button, edit this file.
//
// Each builder leads with a bare marker class ("btn", "input", "badge",
// "modal-card", ...). The markers carry no styling of their own; they are
// stable hooks for tests, for the few contextual stylesheet rules that key off
// them (e.g. `.claude-login-subtle-body .input`), and for readability in the
// inspector.
//
// The Tailwind scanner reads the utility names from the literals in this file
// (`@source "./**/*.ts"` in style.css), so classes assembled here are always
// generated. Keep every utility name a contiguous literal -- never build one
// by string interpolation.

// Type + motion fragments shared by the recipes below. Sizes reference the
// --font-size-* role tokens (see style.css) rather than Tailwind's text-*
// steps, so the type scale stays the single source of truth.
const TEXT_BODY_SIZE = "text-(length:--font-size-body)";
const TEXT_HELPER_SIZE = "text-(length:--font-size-helper)";
const TEXT_HEADING_SIZE = "text-(length:--font-size-heading)";

/* ── Button ──────────────────────────────────────────────────────────────────
 * One button system. Variants: primary | secondary | ghost | destructive |
 * ghost-destructive (quiet destructive: danger text, no fill) | inverse |
 * stop (the composer's slate interrupt fill). Options: sm, icon (square),
 * round (circle), selected (accent-tint pressed look), block (full width),
 * extra (appended utilities/markers). States: hover (guarded so a disabled
 * button never tints), :focus-visible, :disabled + [aria-disabled], :active
 * press. */

export type ButtonVariant =
  "primary" | "secondary" | "ghost" | "destructive" | "ghost-destructive" | "inverse" | "stop";

export interface ButtonOptions {
  sm?: boolean;
  icon?: boolean;
  round?: boolean;
  selected?: boolean;
  block?: boolean;
  extra?: string;
}

const BTN_BASE =
  "btn inline-flex items-center justify-center gap-1.5 " +
  `${TEXT_BODY_SIZE} leading-none font-medium whitespace-nowrap cursor-pointer ` +
  "border border-transparent rounded-md " +
  "transition-[color,background-color,border-color] duration-(--dur-base) ease-[ease] " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
  "disabled:opacity-50 disabled:cursor-not-allowed aria-disabled:opacity-50 aria-disabled:cursor-not-allowed " +
  "not-disabled:not-aria-disabled:active:translate-y-px";

const BTN_VARIANTS: Record<ButtonVariant, string> = {
  primary:
    "bg-accent text-on-accent border-accent not-disabled:hover:bg-accent-hover not-disabled:hover:border-accent-hover",
  secondary: "bg-surface text-primary border-default not-disabled:hover:bg-fill-hover",
  ghost: "bg-transparent text-secondary not-disabled:hover:bg-fill-hover not-disabled:hover:text-primary",
  destructive:
    "bg-danger text-on-accent border-danger not-disabled:hover:bg-danger-hover not-disabled:hover:border-danger-hover",
  "ghost-destructive": "bg-transparent text-danger not-disabled:hover:bg-danger-surface",
  inverse:
    "bg-inverse text-on-accent border-inverse not-disabled:hover:bg-inverse-hover not-disabled:hover:border-inverse-hover",
  stop: "bg-stop text-on-accent border-stop not-disabled:hover:bg-stop-hover not-disabled:hover:border-stop-hover",
};

// The selected (accent-tint) palette replaces the variant's colors outright --
// the builder resolves the conflict in code instead of leaning on the cascade.
const BTN_SELECTED = "bg-accent-light text-accent border-accent";

export function buttonClass(variant: ButtonVariant = "secondary", options: ButtonOptions = {}): string {
  const { sm = false, icon = false, round = false, selected = false, block = false, extra = "" } = options;
  const size = icon
    ? sm
      ? "h-[28px] w-[28px] p-0"
      : "h-[34px] w-[34px] p-0"
    : sm
      ? "h-[28px] px-3"
      : "h-[34px] px-3.5";
  // `btn--<variant>` is a bare marker like `btn` (tests find "the primary
  // button" by it) -- interpolating it is fine because it is not a utility the
  // scanner needs to see.
  const parts = [BTN_BASE, `btn--${variant}`, size, selected ? BTN_SELECTED : BTN_VARIANTS[variant]];
  if (round) parts.push("rounded-full");
  if (block) parts.push("w-full");
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}

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

const BADGE_BASE =
  "badge inline-flex items-center gap-1 px-2 py-0.5 " +
  `${TEXT_HELPER_SIZE} font-normal leading-[1.4] whitespace-nowrap ` +
  "border border-transparent rounded-lg";

const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: "bg-surface text-secondary border-default",
  accent: "bg-surface text-accent border-accent",
  danger: "bg-danger-surface text-danger border-danger-border",
  warning: "bg-warning-surface text-warning",
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
