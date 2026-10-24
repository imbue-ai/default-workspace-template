/**
 * The cards at the top of the page: one per provider in use, answering "how much of my subscription am I using".
 *
 * A provider whose harness reports subscription limits gets a subscription card: each window's share used and
 * when it resets, from the freshest reading. A reading is only as new as the last call that carried one, so the
 * card says when it was taken, and a window whose reset time has passed since then shows as reset rather than
 * at its old share. Usage that is not on a subscription (an API key, Imbue's proxy, a billing mode no writer
 * could tell) gets a spend card per provider for this month. Nothing here names a provider it must find: a
 * card exists only for what the data shows.
 */

import type { LimitReading, TotalsRow } from "./api";

/** How far back a limit reading still says something: the longest window a harness reports is a week. */
export const LIMITS_LOOKBACK_MS = 7 * 24 * 3_600_000;

export type LimitLevel = "ok" | "high" | "critical";

export interface LimitWindow {
  key: string;
  label: string;
  /** Share used, 0-100; 0 when the window has reset since the reading. */
  usedPercent: number | null;
  resetsAt: Date | null;
  isResetSinceReading: boolean;
  level: LimitLevel;
  /** The window's length, when fixed, and when its reading was taken: what a usage estimate calibrates on. */
  windowSeconds: number | null;
  observedAt: Date;
  /** The reading as taken, before a reset since it is applied: its share used and when its window reset. */
  readingPercent: number | null;
  readingResetsAt: Date | null;
  /** The window's first reading: the lowest share seen in it and when, which its rise is measured from. */
  firstPercent: number | null;
  firstObservedAt: Date | null;
}

export interface SubscriptionCard {
  kind: "subscription";
  key: string;
  /** The detail item (a section key) the card opens. */
  sectionKey: string;
  title: string;
  windows: LimitWindow[];
  observedAt: Date;
}

export interface SpendCard {
  kind: "spend";
  key: string;
  sectionKey: string;
  title: string;
  spend: number;
  calls: number;
  /** Calls whose cost is not known, which the spend leaves out. */
  unknownCostCalls: number;
  isEstimated: boolean;
}

export type ProviderCard = SubscriptionCard | SpendCard;

/** Display names for the sources and providers seen so far; anything else is shown by its own name. */
const SUBSCRIPTION_TITLES: Record<string, string> = {
  claude: "Claude subscription",
  codex: "ChatGPT subscription (Codex)",
};
const SOURCE_PROVIDERS: Record<string, string> = { claude: "anthropic", codex: "openai" };

/** The title of a provider's subscription usage, named for the harness whose subscription it is. */
export function subscriptionTitle(provider: string): string {
  const source = Object.keys(SOURCE_PROVIDERS).find((candidate) => SOURCE_PROVIDERS[candidate] === provider);
  return source === undefined ? `${providerName(provider)} subscription` : (SUBSCRIPTION_TITLES[source] ?? source);
}
const PROVIDER_NAMES: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI", google: "Google" };

function capitalized(name: string): string {
  return name.length === 0 ? name : name[0].toUpperCase() + name.slice(1);
}

export function providerName(provider: string): string {
  return PROVIDER_NAMES[provider] ?? capitalized(provider);
}

/** "5-hour limit", "Weekly limit", "3-day limit", from the window's length; its own label otherwise. */
export function windowLabel(reading: Pick<LimitReading, "window" | "label" | "window_seconds">): string {
  const seconds = reading.window_seconds;
  if (seconds !== null && seconds > 0) {
    if (seconds % 86_400 === 0) {
      const days = seconds / 86_400;
      if (days === 7) return "Weekly limit";
      if (days === 1) return "Daily limit";
      return `${days}-day limit`;
    }
    if (seconds % 3_600 === 0) return `${seconds / 3_600}-hour limit`;
  }
  return `${reading.label ?? reading.window} limit`;
}

export function limitLevel(percent: number | null): LimitLevel {
  if (percent === null || percent < 75) return "ok";
  return percent < 90 ? "high" : "critical";
}

/** A card's windows outermost first: the longest window rings the others, and one of unknown length goes outside. */
export function ringOrder(windows: readonly LimitWindow[]): LimitWindow[] {
  const length = (window: LimitWindow): number => window.windowSeconds ?? Number.POSITIVE_INFINITY;
  return [...windows].sort((a, b) => length(b) - length(a));
}

const LEVEL_WORDS: Record<Exclude<LimitLevel, "ok">, string> = { high: "Getting close", critical: "Almost out" };
const A_WEEK_SECONDS = 7 * 24 * 3_600;

/**
 * The warning a card gives, from its fullest window that is getting close: "Almost out this week", "Getting close
 * until 7:00 PM". Null when no window is.
 */
export function limitWarning(windows: readonly LimitWindow[], clock: (at: Date) => string): string | null {
  const fullest = windows
    .filter((window) => window.level !== "ok" && !window.isResetSinceReading)
    .sort((a, b) => (b.usedPercent ?? 0) - (a.usedPercent ?? 0))[0];
  if (fullest === undefined || fullest.level === "ok") return null;
  const words = LEVEL_WORDS[fullest.level];
  if ((fullest.windowSeconds ?? 0) >= A_WEEK_SECONDS) return `${words} this week`;
  return fullest.resetsAt === null ? words : `${words} until ${clock(fullest.resetsAt)}`;
}

function subscriptionCards(
  limits: readonly LimitReading[],
  monthRows: readonly TotalsRow[],
  now: Date,
): SubscriptionCard[] {
  const bySource = new Map<string, LimitReading[]>();
  for (const reading of limits) {
    if (now.getTime() - Date.parse(reading.observed_at) > LIMITS_LOOKBACK_MS) continue;
    bySource.set(reading.source, [...(bySource.get(reading.source) ?? []), reading]);
  }
  return [...bySource.entries()].map(([source, readings]) => {
    const windows = [...readings]
      .sort((a, b) => (a.window_seconds ?? Infinity) - (b.window_seconds ?? Infinity))
      .map((reading): LimitWindow => {
        const resetsAt = reading.resets_at === null ? null : new Date(reading.resets_at);
        const isResetSinceReading = resetsAt !== null && resetsAt.getTime() <= now.getTime();
        const usedPercent = isResetSinceReading ? 0 : reading.used_percentage;
        return {
          key: reading.window,
          label: windowLabel(reading),
          usedPercent,
          resetsAt: isResetSinceReading ? null : resetsAt,
          isResetSinceReading,
          level: limitLevel(usedPercent),
          windowSeconds: reading.window_seconds,
          observedAt: new Date(reading.observed_at),
          readingPercent: reading.used_percentage,
          readingResetsAt: resetsAt,
          firstPercent: reading.first_used_percentage ?? null,
          firstObservedAt: reading.first_observed_at ? new Date(reading.first_observed_at) : null,
        };
      });
    const provider = SOURCE_PROVIDERS[source];
    return {
      kind: "subscription",
      key: `subscription:${source}`,
      sectionKey: `subscription:${provider ?? source}`,
      title: SUBSCRIPTION_TITLES[source] ?? `${capitalized(source)} subscription`,
      windows,
      observedAt: new Date(Math.max(...readings.map((reading) => Date.parse(reading.observed_at)))),
    };
  });
}

function spendCards(monthRows: readonly TotalsRow[]): SpendCard[] {
  const byProvider = new Map<string, SpendCard>();
  for (const row of monthRows) {
    const mode = row.group.billing_mode;
    if (mode === "SUBSCRIPTION") continue;
    const provider = row.group.provider ?? "unknown";
    const card = byProvider.get(provider) ?? {
      kind: "spend",
      key: `spend:${provider}`,
      sectionKey: `spend:${provider}`,
      title: `${providerName(provider)} API`,
      spend: 0,
      calls: 0,
      unknownCostCalls: 0,
      isEstimated: false,
    };
    card.calls += row.calls;
    const isBilled = mode === "API_KEY" || mode === "IMBUE_KEY";
    const cost = row.cost_usd === null ? null : Number(row.cost_usd);
    if (isBilled) {
      card.spend += cost ?? 0;
      card.unknownCostCalls += row.unknown_cost_calls;
      if (cost !== null && cost > 0 && row.group.cost_provenance === "ESTIMATED") card.isEstimated = true;
    } else {
      card.unknownCostCalls += row.calls;
    }
    byProvider.set(provider, card);
  }
  return [...byProvider.values()]
    .filter((card) => card.calls > 0)
    .sort((a, b) => b.spend - a.spend || b.calls - a.calls);
}

/**
 * The top cards, subscriptions first: from the freshest limit readings and this month's totals grouped by
 * ``provider``, ``billing_mode`` and ``cost_provenance``.
 */
export function providerCards(
  limits: readonly LimitReading[],
  monthRows: readonly TotalsRow[],
  now: Date,
): ProviderCard[] {
  return [...subscriptionCards(limits, monthRows, now), ...spendCards(monthRows)];
}
