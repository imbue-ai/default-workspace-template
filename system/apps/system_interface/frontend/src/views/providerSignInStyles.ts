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
/** The panel's frame. Both dimensions come from `panelSize` below, per screen.
 *
 *  The mockup uses one 460px panel with a fixed 498px body for every screen. Both parts were
 *  wrong here: one width makes the lane list cramped and the "All set" check marooned, and a
 *  fixed body height gave a two-line confirmation six hundred pixels of white space under it.
 *
 *  Height is CONTENT-driven, not set: the overlay centers rather than stretches, the body has
 *  no height of its own, and the footer sits outside the scroll region -- so the panel is
 *  exactly header + content + footer until the content passes its screen's cap, at which point
 *  the body scrolls and the panel stops growing. */
export const PANEL = "flex flex-col transition-[width] duration-150 ease-out";

/** The body's shared part. Its ceiling comes from `panelSize`, because how much room a screen
 *  deserves is a fact about that screen. */
const BODY = "overflow-y-auto overscroll-contain px-6 pb-5 pt-1";

/** Each screen's width and its body's ceiling.
 *
 *  Four steps on one scale, so drilling in never feels like a new dialog: a confirmation is a
 *  sentence, a method list is a column of rows, a form is a column of fields, and the lane list
 *  is the widest and tallest thing the flow ever shows. The ceilings are per-screen for the
 *  same reason the widths are -- a method list has no business being as tall as the catalog.
 *
 *  Each is `min(px, vh)` so the tallest screen still fits a laptop; the entry screen's FLOOR is
 *  clamped the same way, or on a short viewport a floor above the ceiling would push the panel
 *  off the bottom of the screen. */
export function panelSize(screen: "status" | "menu" | "form" | "chooser"): { width: string; body: string } {
  return {
    status: { width: "w-[440px]", body: `${BODY} max-h-[min(320px,60vh)]` },
    menu: { width: "w-[600px]", body: `${BODY} max-h-[min(420px,62vh)]` },
    form: { width: "w-[640px]", body: `${BODY} max-h-[min(480px,62vh)]` },
    // The one screen the flow always returns to, so it also keeps a floor: a panel that shrinks
    // under the pointer on the way back reads as something having gone wrong.
    chooser: { width: "w-[690px]", body: `${BODY} max-h-[min(560px,62vh)] min-h-[min(380px,45vh)]` },
  }[screen];
}

export const HEADER = "flex items-center gap-1.5 px-6 pb-4 pt-5";
export const TITLE = "text-[1.25rem] font-semibold tracking-[-0.01em] text-primary";
export const BACK_BUTTON =
  "-ml-2 flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-secondary " +
  "hover:bg-fill-hover hover:text-primary cursor-pointer";
export const CLOSE_BUTTON =
  "-mr-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-secondary " +
  "hover:bg-fill-hover hover:text-primary cursor-pointer";

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
/** A link inside the lead. Where to GET the key, said in the sentence that explains the screen
 *  rather than promoted to a numbered step -- "go to a website if you have not already" is not
 *  half of a two-part procedure. */
export const LEAD_LINK = "text-accent underline underline-offset-2 hover:text-accent-hover";

/** The harness this sign-in will run on, pinned top-right of the header.
 *
 *  The mockup makes it a dropdown, because there it is a CHOICE. Here provider -> harness is
 *  fixed, so it is the same line without the control: a fact the screen states, in the place
 *  the mockup put it. */
/** The right-hand end of the header: the harness line and the close button, as one group.
 *
 *  Grouped because both used to carry `ml-auto` and fought over it -- the first pushed the
 *  label right, the second pushed the button further, and the label ended up stranded in the
 *  middle of the gap. One `ml-auto` on the pair pins them together at the edge. */
export const HEADER_END = "ml-auto flex shrink-0 items-center gap-4";
export const RUNS_ON = "inline-flex shrink-0 items-baseline gap-1.5 text-sm";
export const RUNS_ON_PREFIX = "text-tertiary";
/** The harness is the fact worth reading here, so it carries the weight and the full contrast;
 *  "Runs on" is just the label in front of it. */
export const RUNS_ON_NAME = "font-semibold text-primary";

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

/** verifying / success / error, one shape for all three.
 *
 *  The verdict is the whole screen, so it is sized like one: a bigger disc, a heading at full
 *  strength rather than a step back, and a detail line in the body colour. It read as a caption
 *  under a small icon before -- quiet enough to look like an afterthought on a screen that has
 *  nothing else on it. */
export const STATUS = "flex flex-col items-center px-2 py-8 text-center";
const STATUS_DISC = "mb-4 flex h-[64px] w-[64px] items-center justify-center rounded-full";
export const STATUS_DISC_PENDING = `${STATUS_DISC} text-accent`;
export const STATUS_DISC_SUCCESS = `${STATUS_DISC} bg-accent-light text-accent`;
export const STATUS_DISC_ERROR = `${STATUS_DISC} bg-[#fdecea] text-[#8a1c11]`;
export const STATUS_TITLE = "text-[1.375rem] font-semibold tracking-[-0.01em] text-primary";
export const STATUS_DETAIL = "mt-1.5 max-w-[340px] text-[0.9375rem] leading-snug text-secondary";
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
