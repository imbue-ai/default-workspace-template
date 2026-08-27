/**
 * The mockup's class strings, copied verbatim.
 *
 * `prototypes/minds-harness` is the design source for the provider chooser, and this app
 * runs the same Tailwind v4 with the same `@theme` hexes -- `#e4e3df` border, `#2f6b4f`
 * accent, `#202020` primary text, `#8c8c8c` tertiary -- so the mockup's classes port
 * across unchanged. Keeping them here, as literals rather than re-derived CSS, is what
 * makes a later diff against the mockup meaningful.
 *
 * Source, file by file:
 *   PANEL / HEADER / SCROLL / CHOOSER_ROW*  IntroChooserModal.tsx
 *   STEP_*, INPUT, PRIMARY_BTN, OPTION_ROW  ProviderSignInModal.tsx
 *   SECTION_LABEL                           signInShared.tsx
 *   MODAL                                   IntroFlowModal.tsx (panelClassName)
 */

/** IntroFlowModal's panelClassName. */
export const MODAL =
  "overflow-hidden rounded-xl bg-white shadow-[0_1px_3px_rgba(0,0,0,0.06),0_16px_40px_rgba(0,0,0,0.18)]";

export const PANEL = "flex w-[460px] flex-1 flex-col";

export const HEADER = "flex items-center gap-1.5 px-4 pb-3 pt-3";
export const TITLE = "text-[1.0625rem] font-semibold tracking-[-0.005em] text-[#202020]";
export const HEADER_BUTTON =
  "flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[#666666] hover:bg-[#edecea] hover:text-[#202020] cursor-pointer";
/** The ✕ specifically, which the mockup pushes to the far edge. */
export const CLOSE_BUTTON = `-mr-1 ml-auto ${HEADER_BUTTON}`;

/** Fixed-height scroll region: however many providers we list, the panel never grows. */
export const SCROLL = "h-[332px] overflow-y-auto overscroll-contain px-4 pb-3 pt-1";
export const ROW_STACK = "flex flex-col gap-2";

export const CHOOSER_ROW =
  "flex w-full items-center gap-2 rounded-xl border border-[#e4e3df] bg-white p-3 text-left " +
  "shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all hover:border-[#2f6b4f] " +
  "hover:shadow-[0_2px_6px_rgba(0,0,0,0.08)] cursor-pointer";
/** Reserved even when the lane has no mark, so every label lines up. */
export const CHOOSER_ROW_MARK = "flex w-11 shrink-0 items-center justify-center";
export const CHOOSER_ROW_TEXT = "min-w-0 flex-1";
export const CHOOSER_ROW_NAME = "block text-[15px] font-semibold text-[#202020]";
export const CHOOSER_ROW_BODY = "mt-0.5 block text-[13px] leading-snug text-[#202020]";
export const CHOOSER_ROW_CHEVRON = "shrink-0 text-[#8c8c8c]";

/** The alternates / signed-in rows: same grammar, tighter than a chooser row. */
export const OPTION_ROW =
  "flex w-full items-center justify-between gap-2 rounded-lg border border-[#e4e3df] bg-white p-3 " +
  "text-left shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all hover:border-[#2f6b4f] " +
  "hover:shadow-[0_2px_6px_rgba(0,0,0,0.08)] cursor-pointer";
export const OPTION_ROW_NAME = "text-sm font-medium text-[#202020]";
export const OPTION_ROW_DESC = "mt-0.5 text-[0.75rem] text-[#666666]";

export const SECTION_LABEL = "mb-3.5 text-[0.6875rem] font-semibold uppercase tracking-[0.06em] text-[#666666]";

export const BODY = "px-4 pb-4";
export const LEAD = "mb-4 text-sm leading-relaxed text-[#202020]";

export const STEP = "mb-4";
export const STEP_LABEL = "mb-2 flex items-center gap-2 text-[0.8125rem] font-medium text-[#202020]";
export const STEP_NUM =
  "inline-flex h-[18px] w-[18px] items-center justify-center rounded-full bg-[#2f6b4f] " +
  "text-[0.6875rem] font-semibold text-white";
/** A step that is behind you desaturates so the eye lands on the live one. */
export const STEP_DONE = "grayscale opacity-60";
export const STEP_TRANSITION = "transition-all duration-300";

export const INPUT =
  "w-full rounded-lg border border-[#e4e3df] bg-white px-3 py-[9px] text-sm text-[#202020] " +
  "placeholder:text-[#8c8c8c] focus:outline-none focus:border-[#2f6b4f] " +
  "focus:shadow-[0_0_0_3px_rgba(47,107,79,0.15)] font-mono text-[0.8125rem] tracking-[0.01em]";

export const PRIMARY_BTN =
  "rounded-lg border border-[#2f6b4f] bg-[#2f6b4f] px-4 py-2 text-sm font-medium text-white " +
  "transition-colors hover:bg-[#24573f] disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer";

/** The full-width variant the "Open the sign-in page" link uses. */
export const PRIMARY_LINK_BTN =
  "flex w-full items-center justify-center gap-[7px] rounded-lg border border-[#2f6b4f] bg-[#2f6b4f] " +
  "px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-[#24573f] cursor-pointer";

export const SECONDARY_BTN =
  "shrink-0 rounded-lg border border-[#e4e3df] bg-white px-4 py-2 text-sm font-medium text-[#202020] " +
  "transition-colors hover:bg-[#edecea] disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer";

export const FIELD_ROW = "flex items-center gap-2";

/** Secondary prose under a step -- the copy-link fallback and the like. */
export const HINT = "mt-2 text-[0.75rem] leading-snug text-[#8c8c8c]";
export const HINT_ACTION = "underline underline-offset-2 hover:text-[#202020] cursor-pointer";

/** The raw value, shown when a clipboard write was refused. */
export const RAW_VALUE =
  "mt-2 select-all break-all rounded-lg border border-[#e4e3df] bg-[#f3f2ef] p-2 font-mono " +
  "text-[0.6875rem] leading-snug text-[#202020]";

/** The inverted flow's one-time code, and its copy button beside it. */
export const CODE =
  "flex-1 rounded-lg bg-[#f3f2ef] p-3 text-center font-mono text-[22px] tracking-[0.12em] " +
  "text-[#202020] select-all";

export const NOTICE = "mb-3 rounded-lg bg-[#fdecea] p-3 text-[0.8125rem] leading-snug text-[#8a1c11]";
export const APPLYING = "flex items-center gap-2 py-2 text-sm text-[#666666]";
