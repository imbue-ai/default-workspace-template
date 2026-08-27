/**
 * The mockup's class strings, copied verbatim.
 *
 * `prototypes/minds-harness` is the design source for the provider chooser, and this app
 * runs the same Tailwind v4 with the same `@theme` hexes -- `#e4e3df` border, `#2f6b4f`
 * accent, `#202020` primary text, `#666666` secondary, `#8c8c8c` tertiary -- so the
 * mockup's classes port across unchanged. Keeping them here as literals, rather than
 * re-deriving equivalent CSS, is what keeps a later diff against the mockup meaningful.
 *
 * Source, file by file:
 *   PANEL / HEADER / SCROLL / CHOOSER_ROW*   IntroChooserModal.tsx
 *   BODY_*, STEP_*, INPUT, *_BTN, OPTION_ROW,
 *   STATUS_*, FOOTER                         ProviderSignInModal.tsx
 *   SECTION_LABEL                            signInShared.tsx
 *   MODAL                                    IntroFlowModal.tsx (panelClassName)
 */

export const MODAL =
  "overflow-hidden rounded-xl bg-white shadow-[0_1px_3px_rgba(0,0,0,0.06),0_16px_40px_rgba(0,0,0,0.18)]";
/* Half again as big as the mockup on every side. This is the first thing a new workspace
   shows and it carries the decision the whole product hangs on -- the mockup's 460 was sized
   for a screenshot, not for someone actually choosing. Padding scales with it (px-4 -> px-6,
   pt-3 -> pt-5) so the extra room becomes air rather than longer lines. */
export const PANEL = "flex w-[690px] flex-1 flex-col";

export const HEADER = "flex items-center gap-1.5 px-6 pb-4 pt-5";
export const TITLE = "text-[1.0625rem] font-semibold tracking-[-0.005em] text-primary";
export const BACK_BUTTON =
  "-ml-2 flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-secondary " +
  "hover:bg-fill-hover hover:text-primary cursor-pointer";
export const CLOSE_BUTTON =
  "-mr-1 ml-auto flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-secondary " +
  "hover:bg-fill-hover hover:text-primary cursor-pointer";

/** The body.
 *
 *  The entry screen is a FIXED-height scroll region, and every other screen carries that
 *  same height as a MINIMUM. The mockup does this by measuring the chooser once and keeping
 *  it as a floor: the flow always opens there, so anything shorter afterwards reads as the
 *  card shrinking under you. A short form pads to the floor instead. */
export const BODY_SCROLLING = "h-[498px] overflow-y-auto overscroll-contain px-6 pb-5 pt-1";
export const BODY_FLEXING = "min-h-[498px] flex-1 overflow-y-auto px-6 pb-5 pt-1";
export const ROW_STACK = "flex flex-col gap-2";

export const CHOOSER_ROW =
  "flex w-full items-center gap-2 rounded-xl border border-default bg-white p-3 text-left " +
  "shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all hover:border-accent " +
  "hover:shadow-[0_2px_6px_rgba(0,0,0,0.08)] cursor-pointer";
/** Reserved even when a lane has no mark, so every label lines up. */
export const CHOOSER_ROW_MARK = "flex w-11 shrink-0 items-center justify-center";
export const CHOOSER_ROW_TEXT = "min-w-0 flex-1";
export const CHOOSER_ROW_NAME = "block text-[15px] font-semibold text-primary";
export const CHOOSER_ROW_BODY = "mt-0.5 block text-[13px] leading-snug text-primary";
export const CHEVRON = "shrink-0 text-tertiary";

export const OPTION_ROW =
  "flex w-full items-center justify-between gap-2 rounded-lg border border-default bg-white p-3 " +
  "text-left shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all hover:border-accent " +
  "hover:shadow-[0_2px_6px_rgba(0,0,0,0.08)] cursor-pointer";
export const OPTION_ROW_NAME = "text-sm font-medium text-primary";
export const OPTION_ROW_DESC = "mt-0.5 text-[0.75rem] text-secondary";

export const SECTION_LABEL = "mb-3.5 text-[0.6875rem] font-semibold uppercase tracking-[0.06em] text-secondary";
export const LEAD = "mb-4 text-sm leading-relaxed text-primary";

/** A step block, and the desaturation the mockup puts on the one you are past. */
export const STEP = "mb-4 transition-all duration-300";
export const STEP_LAST = "transition-all duration-300";
export const STEP_DIMMED = "grayscale opacity-60";
export const STEP_LABEL = "mb-2 flex items-center gap-2 text-[0.8125rem] font-medium text-primary";
export const STEP_NUM =
  "inline-flex h-[18px] w-[18px] items-center justify-center rounded-full bg-accent " +
  "text-[0.6875rem] font-semibold text-white";

export const INPUT =
  "w-full rounded-lg border border-default bg-white px-3 py-[9px] text-sm text-primary " +
  "placeholder:text-tertiary focus:outline-none focus:border-accent " +
  "focus:shadow-[0_0_0_3px_rgba(47,107,79,0.15)] font-mono text-[0.8125rem] tracking-[0.01em]";
export const FIELD_ROW = "flex items-center gap-2";

export const PRIMARY_BTN =
  "rounded-lg border border-accent bg-accent px-4 py-2 text-sm font-medium text-white " +
  "transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer";
export const PRIMARY_LINK_BTN =
  "flex w-full items-center justify-center gap-[7px] rounded-lg border border-accent bg-accent " +
  "px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-accent-hover cursor-pointer";
export const SECONDARY_BTN =
  "shrink-0 rounded-lg border border-default bg-white px-4 py-2 text-sm font-medium text-primary " +
  "transition-colors hover:bg-fill-hover disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer";

/** The mockup's success footer: one right-aligned action under the body. */
export const FOOTER = "px-6 pb-5 pt-4";
export const FOOTER_ROW = "flex justify-end";

/** Secondary prose under a field or step -- the mockup's `mt-1.5 text-[0.75rem]`. */
export const HINT = "mt-1.5 text-[0.75rem] leading-snug text-tertiary";
export const HINT_ACTION = "underline underline-offset-2 hover:text-primary cursor-pointer";

/** Shown when a clipboard write was refused, so the value is still reachable by hand. */
export const RAW_VALUE =
  "mt-2 select-all break-all rounded-lg border border-default bg-fill-subtle p-2 font-mono " +
  "text-[0.6875rem] leading-snug text-primary";

/** The device flow's one-time code, sized like the old modal's `.claude-login-code`. */
export const CODE =
  "flex-1 rounded-lg bg-fill-subtle p-3 text-center font-mono text-[22px] tracking-[0.12em] " +
  "text-primary select-all";

/** verifying / success / error, one shape for all three. */
export const STATUS = "flex flex-col items-center px-2 py-6 text-center";
const STATUS_DISC = "mb-3.5 flex h-[52px] w-[52px] items-center justify-center rounded-full";
export const STATUS_DISC_PENDING = `${STATUS_DISC} text-accent`;
export const STATUS_DISC_SUCCESS = `${STATUS_DISC} bg-accent-light text-accent`;
export const STATUS_DISC_ERROR = `${STATUS_DISC} bg-[#fdecea] text-[#8a1c11]`;
export const STATUS_TITLE = "text-[0.9375rem] font-semibold text-secondary";
export const STATUS_DETAIL = "mt-1 max-w-[320px] text-sm text-tertiary";
/** The provider's own mark, under the success check -- so "signed in" names WHICH. */
export const STATUS_MARK = "mt-3 flex items-center justify-center gap-2 text-[0.75rem] text-tertiary";

/** A signed-in account: the OptionRow's frame without its affordances, because the row is a
 *  listed fact rather than somewhere to navigate. Its actions carry the interactivity. */
export const ACCOUNT_ROW =
  "flex w-full items-center gap-2 rounded-lg border border-default bg-white p-3 text-left " +
  "shadow-[0_1px_2px_rgba(0,0,0,0.04)]";
export const ROW_ACTION =
  "shrink-0 rounded-md px-2 py-1 text-[0.75rem] text-secondary transition-colors " +
  "hover:bg-fill-hover hover:text-primary cursor-pointer";

// --- The API-key screen's provider dropdown (ProviderSignInModal's ProviderKeyDropdown) ---

export const PICKER_TRIGGER =
  "flex w-full items-center justify-between gap-2 rounded-lg border border-default bg-white px-3 " +
  "py-[9px] text-left text-sm transition-colors hover:border-accent cursor-pointer";
export const PICKER_TRIGGER_VALUE = "flex min-w-0 items-baseline gap-2";
export const PICKER_TRIGGER_NAME = "truncate text-primary";
export const PICKER_TRIGGER_ENV = "shrink-0 font-mono text-[0.6875rem] text-tertiary";
export const PICKER_TRIGGER_EMPTY = "text-tertiary";
export const PICKER_CARET = "shrink-0 text-tertiary transition-transform";
export const PICKER_CARET_OPEN = "rotate-180";

/** Swallows the outside click that closes the menu, so it never reaches the modal beneath. */
export const PICKER_BACKDROP = "fixed inset-0 z-[210] cursor-default";
/** Pinned under the trigger. The panel is overflow-hidden, so an in-panel popover would be
 *  clipped -- the mockup portals this to <body> for the same reason. */
export const PICKER_MENU =
  "fixed z-[211] max-h-[280px] overflow-y-auto overscroll-contain rounded-lg border " +
  "border-default bg-white p-1 shadow-[0_2px_6px_rgba(0,0,0,0.08),0_12px_32px_rgba(0,0,0,0.16)]";
export const PICKER_OPTION =
  "flex w-full items-center justify-between gap-3 rounded-md px-1.5 py-1.5 text-left cursor-pointer";
export const PICKER_OPTION_IDLE = "hover:bg-[#f4f3f0]";
export const PICKER_OPTION_ACTIVE = "bg-accent-light";
export const PICKER_OPTION_NAME = "truncate text-sm";
export const PICKER_OPTION_NAME_ACTIVE = "truncate text-sm text-accent";
