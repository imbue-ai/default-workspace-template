/**
 * The theme's metrics as behaviour reads them (desktop-interface plan section 6.6): every pixel
 * value the geometry and the gestures need is a token in ``theme/default.css`` (a retro theme may
 * redeclare one), read from the root element's computed style once at boot and again whenever the
 * render mode or the desktop's theme changes, and
 * handed on as one frozen record. No metric is a literal in TypeScript (``test_project_ratchets``
 * holds that for the views and the reducers); the phone breakpoint is the one exception in the
 * other direction, a TypeScript constant applied as a media query that sets ``data-phone``.
 */

/** The viewport the shell renders its phone layout in (plan-phone-interface.md), whichever way round: a short side
 *  at most PHONE_MAX_SHORT_SIDE_PX and a long side at most PHONE_MAX_LONG_SIDE_PX. Rotating a phone keeps the layout;
 *  no tablet gets it, and neither does a desktop window that is short but wide. */
export const PHONE_MAX_SHORT_SIDE_PX = 500;
export const PHONE_MAX_LONG_SIDE_PX = 1000;

export const PHONE_MEDIA_QUERY =
  `(max-width: ${PHONE_MAX_SHORT_SIDE_PX}px) and (max-height: ${PHONE_MAX_LONG_SIDE_PX}px), ` +
  `(max-height: ${PHONE_MAX_SHORT_SIDE_PX}px) and (max-width: ${PHONE_MAX_LONG_SIDE_PX}px)`;
export const TOUCH_MEDIA_QUERY = "(pointer: coarse)";

export const PHONE_ATTRIBUTE = "data-phone";
export const TOUCH_ATTRIBUTE = "data-touch";

export interface ThemeMetrics {
  readonly titleBarHeight: number;
  readonly taskbarHeight: number;
  readonly cellWidth: number;
  readonly cellHeight: number;
  readonly gridInset: number;
  readonly windowMinWidth: number;
  readonly windowMinHeight: number;
  readonly titleMinVisible: number;
  readonly snapThreshold: number;
  readonly unsnapDistance: number;
  readonly dragThreshold: number;
  readonly touchTarget: number;
  readonly floatingEntrySize: number;
  readonly floatingEntryInsetX: number;
  readonly floatingEntryInsetY: number;
}

const TOKEN_BY_METRIC: Readonly<Record<keyof ThemeMetrics, string>> = {
  titleBarHeight: "--desk-title-bar-height",
  taskbarHeight: "--desk-taskbar-height",
  cellWidth: "--desk-cell-width",
  cellHeight: "--desk-cell-height",
  gridInset: "--desk-grid-inset",
  windowMinWidth: "--desk-window-min-width",
  windowMinHeight: "--desk-window-min-height",
  titleMinVisible: "--desk-title-min-visible",
  snapThreshold: "--desk-snap-threshold",
  unsnapDistance: "--desk-unsnap-distance",
  dragThreshold: "--desk-drag-threshold",
  touchTarget: "--desk-touch-target",
  floatingEntrySize: "--desk-floating-entry-size",
  floatingEntryInsetX: "--desk-floating-entry-inset-x",
  floatingEntryInsetY: "--desk-floating-entry-inset-y",
};

/** Raised when a token the metrics need is missing from the computed style or is not a pixel length. */
export class ThemeMetricsError extends Error {}

/** The pixels a token's value names (``"36px"`` reads as 36). */
export function parsePixelLength(token: string, value: string): number {
  const match = /^\s*(-?\d+(?:\.\d+)?)px\s*$/.exec(value);
  if (match === null) {
    throw new ThemeMetricsError(`the theme token ${token} is not a pixel length: ${JSON.stringify(value)}`);
  }
  return Number.parseFloat(match[1]);
}

/** Read every metric off a computed style (the root element's), as one frozen record. */
export function readThemeMetrics(style: Pick<CSSStyleDeclaration, "getPropertyValue">): ThemeMetrics {
  const metrics: Partial<Record<keyof ThemeMetrics, number>> = {};
  for (const [metric, token] of Object.entries(TOKEN_BY_METRIC) as [keyof ThemeMetrics, string][]) {
    metrics[metric] = parsePixelLength(token, style.getPropertyValue(token));
  }
  return Object.freeze(metrics as Required<typeof metrics>) as ThemeMetrics;
}

/** The two render policies, as the media queries answer them right now. */
export interface RenderModes {
  /** The phone layout (a bar, a home grid, and one window at a time) instead of the desktop. */
  readonly isPhone: boolean;
  readonly isTouch: boolean;
}

/** The subset of ``MediaQueryList`` the render modes read, so a test can stand one in. */
export interface MediaQueryLike {
  readonly matches: boolean;
  addEventListener(type: "change", listener: () => void): void;
  removeEventListener(type: "change", listener: () => void): void;
}

export type MatchMedia = (query: string) => MediaQueryLike;

/** The render modes the root element carries now (as ``applyRenderModes`` last stamped them). */
export function currentRenderModes(root: Element): RenderModes {
  return { isPhone: root.hasAttribute(PHONE_ATTRIBUTE), isTouch: root.hasAttribute(TOUCH_ATTRIBUTE) };
}

/** Stamp the render modes onto the root element, which every style keys off. */
export function applyRenderModes(root: Element, modes: RenderModes): void {
  if (modes.isPhone) root.setAttribute(PHONE_ATTRIBUTE, "");
  else root.removeAttribute(PHONE_ATTRIBUTE);
  if (modes.isTouch) root.setAttribute(TOUCH_ATTRIBUTE, "");
  else root.removeAttribute(TOUCH_ATTRIBUTE);
}

/**
 * Follow the two media queries for the life of the page: the root element carries
 * ``data-phone`` and ``data-touch`` while each matches, and ``onChange`` is told the modes and
 * the metrics re-read under them after every change (and once, on subscribe). Answers a function
 * that stops following.
 */
export function followRenderModes(
  root: HTMLElement,
  matchMedia: MatchMedia,
  readStyle: (element: HTMLElement) => Pick<CSSStyleDeclaration, "getPropertyValue">,
  onChange: (modes: RenderModes, metrics: ThemeMetrics) => void,
): () => void {
  const phone = matchMedia(PHONE_MEDIA_QUERY);
  const touch = matchMedia(TOUCH_MEDIA_QUERY);
  const apply = (): void => {
    const modes: RenderModes = { isPhone: phone.matches, isTouch: touch.matches };
    applyRenderModes(root, modes);
    onChange(modes, readThemeMetrics(readStyle(root)));
  };
  phone.addEventListener("change", apply);
  touch.addEventListener("change", apply);
  apply();
  return () => {
    phone.removeEventListener("change", apply);
    touch.removeEventListener("change", apply);
  };
}
