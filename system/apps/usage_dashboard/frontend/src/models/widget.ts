/**
 * The Usage widget's model (app-widgets plan section 7): what the small view on the desktop shows, read
 * from the same cards the page opens on (``providerCards``), so the widget and the page can never disagree on a
 * limit's level or on whether a window has reset since its reading.
 *
 * It derives what every look draws from: a row per provider with data (a subscription's shortest window, the 5-hour
 * limit, and its longest, the weekly one; a pay-as-you-go provider's spend this month), a ring pair per subscription
 * (the longest window outside, the shortest inside), and, with no subscription, one pill of the month's total spend.
 * The views draw them in the look the workspace picked (``widgetLook.ts``).
 *
 * The model reads only what those need (the limit readings, this month's totals, the collector's health, and which
 * look the workspace picked for the widget), again shortly after every new call on the collector's stream and every
 * minute, since a reset time passing changes what a row shows with no call at all.
 */

import { ReconnectBackoff } from "@imbue/workspace-ui/src/models/backoff";
import { fetchCollectorHealth, fetchLimits, fetchTotals, isUnreachable } from "./api";
import { EVENT_DEBOUNCE_MS, STREAM_URL } from "./dashboard";
import type { StreamLike } from "./dashboard";
import { formatClock, formatDollars } from "./format";
import { LIMITS_LOOKBACK_MS, providerCards, providerName } from "./providers";
import type { LimitLevel, LimitWindow, ProviderCard } from "./providers";
import { windowStart } from "./windows";
import { DEFAULT_WIDGET_LOOK, readWidgetLook } from "./widgetLook";
import type { WidgetLook } from "./widgetLook";

/** How often the widget reads again with no call to prompt it: a minute, so a passed reset shows within one. */
export const WIDGET_REFRESH_MS = 60_000;

/** One subscription's row: its shortest window, and its longest when it has more than one. */
export interface LimitRow {
  readonly kind: "limit";
  readonly key: string;
  readonly name: string;
  readonly primary: LimitWindow;
  readonly secondary: LimitWindow | null;
  /** The detail a hover asks for: each window's reset, and when the reading was taken. */
  readonly tooltip: string;
  /** When the reading was taken ("as of 1:51 PM"). */
  readonly asOf: string;
}

/** One pay-as-you-go provider's row: this month's spend. */
export interface SpendRow {
  readonly kind: "spend";
  readonly key: string;
  readonly name: string;
  readonly spend: string;
  readonly tooltip: string;
}

export type WidgetRow = LimitRow | SpendRow;

/** One subscription's rings in the collapsed widget: its shortest window (the 5-hour limit) inside, its longest (the
 *  weekly one) outside when it has more than one. */
export interface RingPair {
  readonly key: string;
  readonly name: string;
  readonly inner: LimitWindow;
  readonly outer: LimitWindow | null;
  /** The detail a hover asks for: the subscription, each window's use and reset, and the reading's time. */
  readonly tooltip: string;
}

/** The collapsed widget's one figure, and the level it is drawn at. */
export interface WidgetPill {
  readonly text: string;
  readonly level: LimitLevel;
  readonly tooltip: string;
}

/** A subscription's short name, from the source its card is keyed by ("Claude", "Codex"). */
function subscriptionName(card: ProviderCard): string {
  const source = card.key.replace(/^subscription:/, "");
  return source.length === 0 ? card.title : source[0].toUpperCase() + source.slice(1);
}

/** What a window says in a tooltip: its name, the share used, and when it resets ("5-hour limit: 61% used, resets
 *  5:00 PM"). */
export function windowText(window: LimitWindow): string {
  if (window.isResetSinceReading) return `${window.label}: reset since the last reading`;
  const used = window.usedPercent === null ? "unknown" : `${Math.round(window.usedPercent)}% used`;
  return window.resetsAt === null
    ? `${window.label}: ${used}`
    : `${window.label}: ${used}, resets ${resetText(window)}`;
}

/** When a window resets, as briefly as tells it apart: the clock for one of hours, the weekday and hour for a longer
 *  one ("Mon 9 AM"). */
export function resetText(window: LimitWindow): string {
  if (window.isResetSinceReading) return "reset";
  if (window.resetsAt === null) return "";
  if (/-hour limit$/.test(window.label)) return formatClock(window.resetsAt);
  return window.resetsAt.toLocaleString(undefined, { weekday: "short", hour: "numeric" });
}

/** A window's name in a row: its label without the trailing "limit" ("5-hour", "Weekly"). */
export function windowName(window: LimitWindow): string {
  return window.label.replace(/ limit$/, "");
}

/** The rows the expanded widget draws, in the page's card order. */
export function widgetRows(cards: readonly ProviderCard[]): WidgetRow[] {
  return cards.flatMap((card): WidgetRow[] => {
    if (card.kind === "spend") {
      return [
        {
          kind: "spend",
          key: card.key,
          name: card.title.replace(/ API$/, ""),
          spend: `${card.isEstimated ? "~" : ""}${formatDollars(card.spend)}`,
          tooltip: `${card.title} this month: ${card.calls} calls`,
        },
      ];
    }
    const primary = card.windows[0];
    if (primary === undefined) return [];
    const last = card.windows[card.windows.length - 1];
    return [
      {
        kind: "limit",
        key: card.key,
        name: subscriptionName(card),
        primary,
        secondary: last === primary ? null : last,
        tooltip: [...card.windows.map(windowText), `as of ${formatClock(card.observedAt)}`].join(" · "),
        asOf: `as of ${formatClock(card.observedAt)}`,
      },
    ];
  });
}

/** How much of a ring is drawn, 0 to 1: none for a window reset since its reading or of unknown use. */
export function ringFraction(window: LimitWindow): number {
  if (window.isResetSinceReading || window.usedPercent === null) return 0;
  return Math.min(1, Math.max(0, window.usedPercent / 100));
}

/** The collapsed widget's rings, one pair per subscription, in the page's card order. */
export function widgetRings(cards: readonly ProviderCard[]): RingPair[] {
  return cards.flatMap((card): RingPair[] => {
    if (card.kind !== "subscription") return [];
    const inner = card.windows[0];
    if (inner === undefined) return [];
    const last = card.windows[card.windows.length - 1];
    const name = subscriptionName(card);
    return [
      {
        key: card.key,
        name,
        inner,
        outer: last === inner ? null : last,
        tooltip: [name, ...card.windows.map(windowText), `as of ${formatClock(card.observedAt)}`].join(" · "),
      },
    ];
  });
}

/** The collapsed widget's figure: the most used subscription window not reset since its reading, with its
 *  subscription's short name (an initial would not tell Claude from Codex); else the total spend this month; null
 *  with nothing to show. */
export function widgetPill(cards: readonly ProviderCard[]): WidgetPill | null {
  let worst: { name: string; window: LimitWindow } | null = null;
  for (const card of cards) {
    if (card.kind !== "subscription") continue;
    for (const window of card.windows) {
      if (window.isResetSinceReading || window.usedPercent === null) continue;
      if (worst === null || window.usedPercent > (worst.window.usedPercent ?? 0)) {
        worst = { name: subscriptionName(card), window };
      }
    }
  }
  if (worst !== null) {
    return {
      text: `${worst.name} ${Math.round(worst.window.usedPercent ?? 0)}%`,
      level: worst.window.level,
      tooltip: `${worst.name} ${windowText(worst.window)}`,
    };
  }
  const spendCards = cards.filter((card) => card.kind === "spend");
  if (spendCards.length === 0) return null;
  const total = spendCards.reduce((sum, card) => sum + card.spend, 0);
  return {
    text: formatDollars(total),
    level: "ok",
    tooltip: spendCards
      .map((card) => `${providerName(card.key.replace(/^spend:/, ""))}: ${formatDollars(card.spend)}`)
      .join(" · "),
  };
}

export interface WidgetDeps {
  now: () => Date;
  redraw: () => void;
  openStream: (url: string) => StreamLike;
}

export class WidgetModel {
  /** Null until the first read answers. */
  cards: ProviderCard[] | null = null;
  isLive = false;
  isCatchingUp = false;
  isCollectorDown = false;
  /** How the workspace's widget looks, as last read; a pick on the Usage page sets it at once. */
  look: WidgetLook = DEFAULT_WIDGET_LOOK;

  private stream: StreamLike | null = null;
  private readonly streamBackoff = new ReconnectBackoff();
  private eventRefresh: ReturnType<typeof setTimeout> | null = null;
  private periodic: ReturnType<typeof setInterval> | null = null;
  /** How many picks have been heard over the look's channel: a read begun before one keeps its older look out. */
  private lookPushes = 0;

  constructor(private readonly deps: WidgetDeps) {}

  start(): void {
    void this.refresh();
    this.connectStream();
    this.periodic = setInterval(() => void this.refresh(), WIDGET_REFRESH_MS);
  }

  stop(): void {
    if (this.periodic !== null) clearInterval(this.periodic);
    if (this.eventRefresh !== null) clearTimeout(this.eventRefresh);
    this.stream?.close();
    this.stream = null;
  }

  /** Whether there is no usage to show at all: read, and no provider has any. */
  get isEmpty(): boolean {
    return this.cards !== null && this.cards.length === 0 && !this.isCatchingUp;
  }

  /** A pick made on the Usage page in this browser, heard over the look's channel. */
  takeLook(look: WidgetLook): void {
    this.lookPushes += 1;
    this.look = look;
    this.deps.redraw();
  }

  async refresh(): Promise<void> {
    const now = this.deps.now();
    const lookPushesBefore = this.lookPushes;
    try {
      const [limits, monthRows, health] = await Promise.all([
        fetchLimits(new Date(now.getTime() - LIMITS_LOOKBACK_MS)),
        fetchTotals({ groupBy: ["provider", "billing_mode", "cost_provenance"], since: windowStart("month", now) }),
        fetchCollectorHealth(),
      ]);
      this.cards = providerCards(limits, monthRows, now);
      this.isCatchingUp = health.catching_up;
      this.isCollectorDown = false;
    } catch (error) {
      this.isCollectorDown = isUnreachable(error);
      if (!this.isCollectorDown) console.warn("[usage] could not read the widget's figures:", error);
    }
    const look = await readWidgetLook();
    if (look !== null && this.lookPushes === lookPushesBefore) this.look = look;
    this.deps.redraw();
  }

  private scheduleEventRefresh(): void {
    if (this.eventRefresh !== null) return;
    this.eventRefresh = setTimeout(() => {
      this.eventRefresh = null;
      void this.refresh();
    }, EVENT_DEBOUNCE_MS);
  }

  private connectStream(): void {
    const stream = this.deps.openStream(STREAM_URL);
    this.stream = stream;
    stream.onopen = () => {
      this.isLive = true;
      this.streamBackoff.reset();
      this.deps.redraw();
    };
    stream.addEventListener("model_call", () => this.scheduleEventRefresh());
    stream.onerror = () => {
      stream.close();
      if (this.stream !== stream) return;
      this.stream = null;
      this.isLive = false;
      this.deps.redraw();
      setTimeout(() => this.connectStream(), this.streamBackoff.nextDelay());
    };
  }
}
