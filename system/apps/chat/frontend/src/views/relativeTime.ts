/**
 * How long ago something happened, as the chat list and the open chat write it: "5m" or "3d" in a list row,
 * "3 days ago" in a sentence, and the exact local date and time for a tooltip.
 *
 * `Age` draws one of these and keeps it current on its own, rewriting just its text, so a chat left open for an
 * hour never says "now" and no page redraws to age a label.
 */

import m from "mithril";

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const MONTH_MS = 30 * DAY_MS;
const YEAR_MS = 365 * DAY_MS;
const AGE_REFRESH_MS = 30_000;

type AgeUnit = "minute" | "hour" | "day" | "month" | "year";

const SHORT_SUFFIX_BY_UNIT: Readonly<Record<AgeUnit, string>> = {
  minute: "m",
  hour: "h",
  day: "d",
  month: "mo",
  year: "y",
};

const LONG_FORMAT = new Intl.RelativeTimeFormat("en", { numeric: "always" });

/** The age in its largest whole unit, or null under a minute (or for a time ahead of this clock). */
function ageOf(atMs: number, nowMs: number): { value: number; unit: AgeUnit } | null {
  const elapsed = nowMs - atMs;
  if (elapsed < MINUTE_MS) return null;
  if (elapsed < HOUR_MS) return { value: Math.floor(elapsed / MINUTE_MS), unit: "minute" };
  if (elapsed < DAY_MS) return { value: Math.floor(elapsed / HOUR_MS), unit: "hour" };
  if (elapsed < MONTH_MS) return { value: Math.floor(elapsed / DAY_MS), unit: "day" };
  if (elapsed < YEAR_MS) return { value: Math.floor(elapsed / MONTH_MS), unit: "month" };
  return { value: Math.floor(elapsed / YEAR_MS), unit: "year" };
}

/** "now", "5m", "23h", "3d", "2mo", "1y". */
export function shortAge(atMs: number, nowMs: number): string {
  const age = ageOf(atMs, nowMs);
  return age === null ? "now" : `${age.value}${SHORT_SUFFIX_BY_UNIT[age.unit]}`;
}

/** "just now", "5 minutes ago", "1 day ago", "3 days ago". */
export function longAge(atMs: number, nowMs: number): string {
  const age = ageOf(atMs, nowMs);
  return age === null ? "just now" : LONG_FORMAT.format(-age.value, age.unit);
}

/** The moment itself, in the viewer's locale and time zone. */
export function exactTime(atMs: number): string {
  return new Date(atMs).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export interface AgeAttrs {
  atMs: number;
  format: (atMs: number, nowMs: number) => string;
  /** The words before the age in its tooltip, e.g. "Last message". */
  tooltipLead: string;
  class: string;
}

export function Age(): m.Component<AgeAttrs> {
  let current: AgeAttrs | null = null;
  let element: HTMLElement | null = null;
  let timer: ReturnType<typeof setInterval> | null = null;
  // The text is written here, never rendered as a child: the timer rewrites it between redraws, so a text node
  // Mithril owned would go stale under it.
  function writeAge(): void {
    if (current !== null && element !== null) element.textContent = current.format(current.atMs, Date.now());
  }
  return {
    oncreate(vnode) {
      current = vnode.attrs;
      element = vnode.dom as HTMLElement;
      writeAge();
      timer = setInterval(writeAge, AGE_REFRESH_MS);
    },
    onupdate(vnode) {
      current = vnode.attrs;
      writeAge();
    },
    onremove() {
      if (timer !== null) clearInterval(timer);
    },
    view(vnode) {
      const { atMs, tooltipLead } = vnode.attrs;
      return m("span", { class: vnode.attrs.class, title: `${tooltipLead} ${exactTime(atMs)}` });
    },
  };
}
