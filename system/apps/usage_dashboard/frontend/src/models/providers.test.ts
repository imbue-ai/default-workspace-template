import { describe, expect, it } from "vitest";

import type { LimitReading } from "./api";
import { totalsRow } from "../testing/rows";
import { type LimitWindow, limitLevel, limitWarning, providerCards, ringOrder, windowLabel } from "./providers";

const NOW = new Date("2026-10-22T20:00:00Z");

function reading(patch: Partial<LimitReading>): LimitReading {
  return {
    source: "claude",
    window: "five_hour",
    label: "5h",
    used_percentage: 28,
    resets_at: "2026-10-22T22:30:00Z",
    window_seconds: 18_000,
    observed_at: "2026-10-22T19:45:00Z",
    first_used_percentage: 20,
    first_observed_at: "2026-10-22T18:30:00Z",
    ...patch,
  };
}

describe("providerCards", () => {
  it("gives a subscription card per reporting source, shortest window first", () => {
    const [card] = providerCards(
      [reading({ window: "seven_day", label: "7d", used_percentage: 8, window_seconds: 604_800 }), reading({})],
      [],
      NOW,
    );

    expect(card.kind).toBe("subscription");
    if (card.kind !== "subscription") return;
    expect(card.title).toBe("Claude subscription");
    expect(card.windows.map((window) => [window.label, window.usedPercent])).toEqual([
      ["5-hour limit", 28],
      ["Weekly limit", 8],
    ]);
    expect(card.observedAt).toEqual(new Date("2026-10-22T19:45:00Z"));
  });

  it("shows a window whose reset has passed since the reading as reset", () => {
    const [card] = providerCards([reading({ used_percentage: 96, resets_at: "2026-10-22T19:50:00Z" })], [], NOW);

    if (card.kind !== "subscription") throw new Error("expected a subscription card");
    expect(card.windows[0]).toMatchObject({ usedPercent: 0, isResetSinceReading: true, level: "ok" });
  });

  it("carries each window's first reading, and none from a collector that kept none", () => {
    const [card] = providerCards([reading({})], [], NOW);
    const [older] = providerCards([reading({ first_used_percentage: null, first_observed_at: null })], [], NOW);

    if (card.kind !== "subscription" || older.kind !== "subscription") throw new Error("expected subscription cards");
    expect(card.windows[0]).toMatchObject({
      firstPercent: 20,
      firstObservedAt: new Date("2026-10-22T18:30:00Z"),
    });
    expect(older.windows[0]).toMatchObject({ firstPercent: null, firstObservedAt: null });
  });

  it("drops a source whose last reading is over a week old", () => {
    expect(providerCards([reading({ observed_at: "2026-10-14T19:00:00Z" })], [], NOW)).toEqual([]);
  });

  it("keeps a subscription's calls off the spend cards", () => {
    const cards = providerCards(
      [reading({})],
      [totalsRow({ group: { provider: "anthropic", billing_mode: "SUBSCRIPTION" }, calls: 9, cost_usd: "3.5" })],
      NOW,
    );

    expect(cards).toHaveLength(1);
    expect(cards[0]).toMatchObject({ kind: "subscription" });
  });

  it("gives a spend card per provider used without a subscription, counting unknown costs", () => {
    const cards = providerCards(
      [],
      [
        totalsRow({
          group: { provider: "anthropic", billing_mode: "API_KEY", cost_provenance: "REPORTED" },
          calls: 4,
          cost_usd: "2",
        }),
        totalsRow({
          group: { provider: "anthropic", billing_mode: "UNKNOWN", cost_provenance: "ESTIMATED" },
          calls: 3,
          cost_usd: "1",
        }),
        totalsRow({
          group: { provider: "openai", billing_mode: "API_KEY", cost_provenance: "ESTIMATED" },
          calls: 1,
          cost_usd: "0.5",
        }),
      ],
      NOW,
    );

    expect(cards.map((card) => card.title)).toEqual(["Anthropic API", "OpenAI API"]);
    expect(cards[0]).toMatchObject({ kind: "spend", spend: 2, calls: 7, unknownCostCalls: 3, isEstimated: false });
    expect(cards[1]).toMatchObject({ isEstimated: true });
  });
});

describe("windowLabel", () => {
  it("names a window by its length, and by its label without one", () => {
    expect(windowLabel({ window: "five_hour", label: "5h", window_seconds: 18_000 })).toBe("5-hour limit");
    expect(windowLabel({ window: "seven_day", label: "7d", window_seconds: 604_800 })).toBe("Weekly limit");
    expect(windowLabel({ window: "overage", label: null, window_seconds: null })).toBe("overage limit");
  });
});

describe("limitLevel", () => {
  it("is high from 75% and critical from 90%", () => {
    expect([74, 75, 89, 90].map(limitLevel)).toEqual(["ok", "high", "high", "critical"]);
  });
});

function limitWindow(overrides: Partial<LimitWindow>): LimitWindow {
  return {
    key: "five_hour",
    label: "5-hour limit",
    usedPercent: 10,
    resetsAt: new Date("2026-10-22T23:00:00Z"),
    isResetSinceReading: false,
    level: "ok",
    windowSeconds: 18_000,
    observedAt: NOW,
    readingPercent: 10,
    readingResetsAt: new Date("2026-10-22T23:00:00Z"),
    firstPercent: 4,
    firstObservedAt: new Date("2026-10-22T19:00:00Z"),
    ...overrides,
  };
}

describe("ringOrder", () => {
  it("puts the longest window outermost, and one of unknown length outside the rest", () => {
    const fiveHour = limitWindow({ key: "five_hour", windowSeconds: 18_000 });
    const week = limitWindow({ key: "seven_day", windowSeconds: 604_800 });
    const unknown = limitWindow({ key: "monthly", windowSeconds: null });
    expect(ringOrder([fiveHour, unknown, week]).map((window) => window.key)).toEqual([
      "monthly",
      "seven_day",
      "five_hour",
    ]);
  });
});

describe("limitWarning", () => {
  const clock = (at: Date): string => at.toISOString().slice(11, 16);

  it("says nothing while every window has room", () => {
    expect(limitWarning([limitWindow({})], clock)).toBeNull();
  });

  it("names the week for a weekly window and the reset time for a shorter one", () => {
    expect(limitWarning([limitWindow({ usedPercent: 93, level: "critical", windowSeconds: 604_800 })], clock)).toBe(
      "Almost out this week",
    );
    expect(limitWarning([limitWindow({ usedPercent: 80, level: "high" })], clock)).toBe("Getting close until 23:00");
  });

  it("warns about the fullest window, and not one that has reset since its reading", () => {
    const windows = [
      limitWindow({ key: "five_hour", usedPercent: 82, level: "high" }),
      limitWindow({ key: "seven_day", usedPercent: 93, level: "critical", windowSeconds: 604_800 }),
    ];
    expect(limitWarning(windows, clock)).toBe("Almost out this week");
    expect(
      limitWarning([limitWindow({ usedPercent: 95, level: "critical", isResetSinceReading: true })], clock),
    ).toBeNull();
  });
});
