/**
 * The theme's metrics as behaviour reads them (desktop-interface plan section 6.6): every pixel
 * value the geometry and the gestures need is a token in ``theme/default.css``, read from the
 * root element's computed style once at boot and again whenever the render mode changes, and
 * handed on as one frozen record. No metric is a literal in TypeScript (``test_project_ratchets``
 * holds that for the views and the reducers); the compact breakpoint is the one exception in the
 * other direction, a TypeScript constant applied as a media query that sets ``data-compact``.
 */

/** The viewport width under which the desktop renders in compact mode (contracts.md section 11). */
export const COMPACT_MAX_WIDTH_PX = 700;

export const COMPACT_MEDIA_QUERY = `(max-width: ${COMPACT_MAX_WIDTH_PX}px)`;
export const TOUCH_MEDIA_QUERY = "(pointer: coarse)";

export const COMPACT_ATTRIBUTE = "data-compact";
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
  readonly isCompact: boolean;
  readonly isTouch: boolean;
}

/** The subset of ``MediaQueryList`` the render modes read, so a test can stand one in. */
export interface MediaQueryLike {
  readonly matches: boolean;
  addEventListener(type: "change", listener: () => void): void;
  removeEventListener(type: "change", listener: () => void): void;
}

export type MatchMedia = (query: string) => MediaQueryLike;

/** Stamp the render modes onto the root element, which every style keys off. */
export function applyRenderModes(root: Element, modes: RenderModes): void {
  if (modes.isCompact) root.setAttribute(COMPACT_ATTRIBUTE, "");
  else root.removeAttribute(COMPACT_ATTRIBUTE);
  if (modes.isTouch) root.setAttribute(TOUCH_ATTRIBUTE, "");
  else root.removeAttribute(TOUCH_ATTRIBUTE);
}

/**
 * Follow the two media queries for the life of the page: the root element carries
 * ``data-compact`` and ``data-touch`` while each matches, and ``onChange`` is told the modes and
 * the metrics re-read under them after every change (and once, on subscribe). Answers a function
 * that stops following.
 */
export function followRenderModes(
  root: HTMLElement,
  matchMedia: MatchMedia,
  readStyle: (element: HTMLElement) => Pick<CSSStyleDeclaration, "getPropertyValue">,
  onChange: (modes: RenderModes, metrics: ThemeMetrics) => void,
): () => void {
  const compact = matchMedia(COMPACT_MEDIA_QUERY);
  const touch = matchMedia(TOUCH_MEDIA_QUERY);
  const apply = (): void => {
    const modes: RenderModes = { isCompact: compact.matches, isTouch: touch.matches };
    applyRenderModes(root, modes);
    onChange(modes, readThemeMetrics(readStyle(root)));
  };
  compact.addEventListener("change", apply);
  touch.addEventListener("change", apply);
  apply();
  return () => {
    compact.removeEventListener("change", apply);
    touch.removeEventListener("change", apply);
  };
}
