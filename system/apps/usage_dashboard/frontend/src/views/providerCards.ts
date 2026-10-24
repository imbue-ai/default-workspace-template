/**
 * The top of the page: one card per provider in use. A subscription card shows how much of each limit is used
 * and when it resets; a spend card shows this month's pay-as-you-go spend.
 */

import m from "mithril";

import { formatCalls, formatClock, formatDollars } from "../models/format";
import {
  type LimitLevel,
  type LimitWindow,
  type ProviderCard,
  type SpendCard,
  type SubscriptionCard,
  limitWarning,
  ringOrder,
} from "../models/providers";
import { pathOf } from "../models/routing";
import { cardTitle } from "./cardTitle";

const CARD_CLASS = "provider-card flex min-w-0 flex-col gap-3 rounded-lg border border-default bg-surface p-4";

// The ring and its share wear the window's level, so a near-full limit reads as one at a glance.
const RING_STROKES: Record<LimitLevel, string> = {
  ok: "stroke-(--color-accent)",
  high: "stroke-(--color-warning)",
  critical: "stroke-(--color-danger)",
};
const LEVEL_TEXT: Record<LimitLevel, string> = { ok: "text-accent", high: "text-warning", critical: "text-danger" };

// The rings in a 120-unit box: each ring STROKE wide, GAP apart, the outermost touching the edge.
const RING_BOX = 120;
const RING_STROKE = 11;
const RING_GAP = 4;

/** "resets 9:30 PM" today, "resets Mon 2:00 PM" another day. */
export function resetText(resetsAt: Date, now: Date): string {
  const time = resetsAt.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (resetsAt.toDateString() === now.toDateString()) return `resets ${time}`;
  return `resets ${resetsAt.toLocaleDateString(undefined, { weekday: "short" })} ${time}`;
}

/** When a reading was taken: the clock today, the weekday and clock before that. */
export function asOfText(observedAt: Date, now: Date): string {
  if (observedAt.toDateString() === now.toDateString()) return formatClock(observedAt);
  return `${observedAt.toLocaleDateString(undefined, { weekday: "short" })} ${formatClock(observedAt)}`;
}

/** "5-hour", "weekly": a window's label as the list beside the rings reads it. */
function shortLabel(window: LimitWindow): string {
  const label = window.label.replace(/ limit$/, "");
  return /^\d/.test(label) ? label : label.charAt(0).toLowerCase() + label.slice(1);
}

function percentText(window: LimitWindow): string {
  return window.usedPercent === null ? "–" : `${Math.round(window.usedPercent)}%`;
}

/** Concentric rings, one per window, the longest outermost, each filled clockwise from the top to its share. */
function limitRings(windows: readonly LimitWindow[]): m.Vnode {
  const center = RING_BOX / 2;
  const innermost = windows[windows.length - 1];
  return m("div", { class: "limit-rings relative w-36 shrink-0" }, [
    m(
      "svg",
      {
        viewBox: `0 0 ${RING_BOX} ${RING_BOX}`,
        class: "block w-full -rotate-90",
        role: "img",
        "aria-label": windows.map((window) => `${shortLabel(window)} ${percentText(window)} used`).join(", "),
      },
      windows.map((window, index) => {
        const radius = center - RING_STROKE / 2 - index * (RING_STROKE + RING_GAP);
        const share = Math.min(100, Math.max(0, window.usedPercent ?? 0));
        return m("g", { key: window.key, "data-window": window.key }, [
          m("circle", {
            cx: center,
            cy: center,
            r: radius,
            fill: "none",
            "stroke-width": RING_STROKE,
            class: "stroke-(--color-fill-hover)",
          }),
          share === 0
            ? null
            : m("circle", {
                class: `limit-ring ${RING_STROKES[window.level]}`,
                cx: center,
                cy: center,
                r: radius,
                fill: "none",
                "stroke-width": RING_STROKE,
                "stroke-linecap": "round",
                pathLength: 100,
                "stroke-dasharray": `${share} 100`,
              }),
        ]);
      }),
    ),
    innermost === undefined
      ? null
      : m("div", { class: "pointer-events-none absolute inset-0 flex flex-col items-center justify-center" }, [
          m("span", { class: "type-heading-lg tabular-nums text-primary" }, percentText(innermost)),
          m("span", { class: "type-helper text-secondary" }, shortLabel(innermost)),
        ]),
  ]);
}

/** One line per window beside the rings: its share, in its level's colour, and when it resets. */
function limitLine(window: LimitWindow, now: Date): m.Vnode {
  const when = window.isResetSinceReading
    ? "reset since the last reading"
    : window.resetsAt === null
      ? null
      : resetText(window.resetsAt, now);
  return m("div", { class: "limit-window type-body text-primary", "data-window": window.key }, [
    m("span", { class: `font-semibold tabular-nums ${LEVEL_TEXT[window.level]}` }, percentText(window)),
    ` ${shortLabel(window)}`,
    when === null ? null : m("span", { class: "text-secondary" }, ` · ${when}`),
  ]);
}

function subscriptionCard(card: SubscriptionCard, now: Date): m.Vnode {
  const windows = ringOrder(card.windows);
  const warning = limitWarning(windows, formatClock);
  const isCritical = windows.some((window) => window.level === "critical" && !window.isResetSinceReading);
  return m("section", { class: CARD_CLASS, "data-card": card.key }, [
    m("div", { class: "flex items-baseline justify-between gap-2" }, [
      cardTitle(card.title, "h2"),
      m(
        "span",
        { class: "shrink-0 whitespace-nowrap type-tag text-faint" },
        `as of ${asOfText(card.observedAt, now)}`,
      ),
    ]),
    m("div", { class: "flex flex-wrap items-center gap-x-6 gap-y-3" }, [
      limitRings(windows),
      m("div", { class: "flex min-w-0 flex-1 basis-40 flex-col gap-2" }, [
        // Innermost first, the window the rings' centre shows.
        [...windows].reverse().map((window) => limitLine(window, now)),
        warning === null
          ? null
          : m("div", { class: `limit-warning type-body ${isCritical ? "text-danger" : "text-warning"}` }, warning),
      ]),
    ]),
  ]);
}

function spendCard(card: SpendCard): m.Vnode {
  return m("section", { class: CARD_CLASS, "data-card": card.key }, [
    m("div", { class: "flex items-baseline justify-between gap-2" }, [
      cardTitle(card.title, "h2"),
      m("span", { class: "shrink-0 whitespace-nowrap type-tag text-faint" }, "this month"),
    ]),
    m("div", { class: "flex items-baseline gap-2" }, [
      m(
        "span",
        { class: "type-heading-lg tabular-nums text-primary" },
        `${card.isEstimated ? "~" : ""}${formatDollars(card.spend)}`,
      ),
      m("span", { class: "type-helper text-secondary" }, formatCalls(card.calls)),
    ]),
    card.unknownCostCalls === 0
      ? null
      : m("div", { class: "type-helper text-warning" }, `+${formatCalls(card.unknownCostCalls)} with unknown cost`),
    card.isEstimated ? m("div", { class: "type-helper text-faint" }, "~ includes costs estimated from tokens") : null,
  ]);
}

/** One card's contents, as the home page and a detail page both show it. */
export function providerCard(card: ProviderCard, now: Date): m.Vnode {
  return card.kind === "subscription" ? subscriptionCard(card, now) : spendCard(card);
}

/** The home page's cards, each a link to its provider's detail page. */
export function providerSection(cards: readonly ProviderCard[] | null, now: Date): m.Vnode {
  if (cards === null) return m("div", { class: "type-helper py-6 text-center text-faint" }, "Loading usage…");
  if (cards.length === 0) {
    return m(
      "div",
      { class: "provider-empty rounded-lg border border-default bg-surface p-4 type-body text-secondary" },
      "No model usage yet this month.",
    );
  }
  return m(
    "div",
    // A subscription card is wider than a spend card, so its rings and their list sit side by side.
    { class: "provider-cards flex flex-wrap gap-3" },
    cards.map((card) =>
      m(
        "a",
        {
          key: card.key,
          href: pathOf({ page: "item", key: card.sectionKey }),
          class:
            `provider-link flex min-w-0 rounded-lg no-underline transition-shadow hover:shadow-md ` +
            `focus-visible:outline-2 focus-visible:outline-accent [&>*]:flex-1 ` +
            (card.kind === "subscription" ? "grow-[3] basis-[24rem]" : "grow-[2] basis-[16rem]"),
          "aria-label": `${card.title}: see details`,
        },
        providerCard(card, now),
      ),
    ),
  );
}
