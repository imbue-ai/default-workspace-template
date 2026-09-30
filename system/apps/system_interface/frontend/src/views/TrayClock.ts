/**
 * The clock at the taskbar's right end: the local time, redrawn every half minute so the
 * minute never lags. Display only.
 */

import m from "mithril";

const TICK_MS = 30_000;

export function formatClock(now: Date): string {
  return now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function TrayClock(): m.Component {
  let timer: number | null = null;
  return {
    oncreate() {
      timer = window.setInterval(() => m.redraw(), TICK_MS);
    },
    onremove() {
      if (timer !== null) window.clearInterval(timer);
    },
    view() {
      return m(
        "span",
        { "data-tray-widget": "clock", class: "tray-clock shrink-0 select-none" },
        formatClock(new Date()),
      );
    },
  };
}
