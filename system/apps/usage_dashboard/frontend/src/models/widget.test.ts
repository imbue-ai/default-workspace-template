import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RECONNECT_CAP_MS } from "@imbue/workspace-ui/src/models/backoff";

import type { LimitReading } from "./api";
import { totalsRow } from "../testing/rows";
import { EVENT_DEBOUNCE_MS, STREAM_URL } from "./dashboard";
import type { StreamLike } from "./dashboard";
import { providerCards } from "./providers";
import { WIDGET_REFRESH_MS, WidgetModel, ringFraction, widgetPill, widgetRings, widgetRows } from "./widget";
import { DEFAULT_WIDGET_LOOK } from "./widgetLook";

const NOW = new Date("2026-10-22T20:00:00Z");

function reading(patch: Partial<LimitReading>): LimitReading {
  return {
    source: "claude",
    window: "five_hour",
    label: "5h",
    used_percentage: 61,
    resets_at: "2026-10-22T22:30:00Z",
    window_seconds: 18_000,
    observed_at: "2026-10-22T19:45:00Z",
    first_used_percentage: null,
    first_observed_at: null,
    ...patch,
  };
}

const WEEKLY = { window: "seven_day", label: "7d", window_seconds: 604_800, resets_at: "2026-10-26T21:00:00Z" };

const API_SPEND = totalsRow({
  group: { provider: "anthropic", billing_mode: "API_KEY", cost_provenance: "ESTIMATED" },
  calls: 12,
  cost_usd: "142.31",
});

describe("widgetRows", () => {
  it("gives a subscription its shortest window as the bar and its longest beside it, and an API its spend", () => {
    const cards = providerCards([reading({}), reading({ ...WEEKLY, used_percentage: 34 })], [API_SPEND], NOW);

    const rows = widgetRows(cards);

    expect(rows.map((row) => [row.kind, row.name])).toEqual([
      ["limit", "Claude"],
      ["spend", "Anthropic"],
    ]);
    const [limit, spend] = rows;
    if (limit.kind !== "limit" || spend.kind !== "spend") throw new Error("rows out of order");
    expect([limit.primary.label, limit.primary.usedPercent, limit.secondary?.usedPercent]).toEqual([
      "5-hour limit",
      61,
      34,
    ]);
    expect(limit.tooltip).toContain("5-hour limit: 61% used, resets");
    expect(limit.tooltip).toContain("as of");
    expect(spend.spend).toBe("~$142.31");
  });

  it("draws nothing for a provider with no data, and one window alone with no figure beside it", () => {
    expect(widgetRows([])).toEqual([]);
    const [row] = widgetRows(providerCards([reading({})], [], NOW));
    expect(row.kind === "limit" ? row.secondary : "not a limit row").toBeNull();
  });
});

describe("widgetPill", () => {
  it("names the most used subscription window not reset since its reading, at its level", () => {
    const cards = providerCards(
      [
        reading({ used_percentage: 96, resets_at: "2026-10-22T19:00:00Z" }),
        reading({ ...WEEKLY, used_percentage: 80 }),
        reading({ source: "codex", used_percentage: 18 }),
      ],
      [API_SPEND],
      NOW,
    );

    // The 96% window reset at 19:00, before now: it is left out, and the weekly 80% is the most used.
    expect(widgetPill(cards)).toMatchObject({ text: "Claude 80%", level: "high" });
  });

  it("falls back to this month's total spend with no subscription, and to nothing with no usage", () => {
    const openai = totalsRow({ group: { provider: "openai", billing_mode: "API_KEY" }, cost_usd: "22.90" });
    expect(widgetPill(providerCards([], [API_SPEND, openai], NOW))).toMatchObject({ text: "$165.21", level: "ok" });
    expect(widgetPill([])).toBeNull();
  });
});

describe("WidgetModel", () => {
  /** A collector that answers ``limits``, no totals, and ``health``; ``status`` for every read when it is set. */
  function stubCollector(
    limits: LimitReading[],
    isCatchingUp: boolean,
    status: number | null = null,
    variant = "rings",
  ): void {
    vi.stubGlobal("fetch", async (url: string) => {
      if (status !== null) return new Response(JSON.stringify({ detail: "down" }), { status });
      const answer = url.includes("/limits")
        ? { limits }
        : url.includes("/totals")
          ? { rows: [] }
          : url.includes("/settings")
            ? { widget_variant: variant }
            : { status: "ok", catching_up: isCatchingUp };
      return new Response(JSON.stringify(answer), { status: 200 });
    });
  }

  function model(): WidgetModel {
    const stream = { addEventListener: () => undefined, onopen: null, onerror: null, close: () => undefined };
    return new WidgetModel({ now: () => NOW, redraw: () => undefined, openStream: () => stream });
  }

  afterEach(() => vi.unstubAllGlobals());

  it("is empty only once a read found no usage and the collector is not still indexing", async () => {
    const widget = model();
    expect(widget.isEmpty).toBe(false);
    stubCollector([], false);
    await widget.refresh();
    expect(widget.isEmpty).toBe(true);

    stubCollector([], true);
    await widget.refresh();
    expect([widget.isEmpty, widget.isCatchingUp]).toEqual([false, true]);

    stubCollector([reading({})], false);
    await widget.refresh();
    expect(widget.isEmpty).toBe(false);
    expect(widgetRows(widget.cards ?? []).map((row) => row.name)).toEqual(["Claude"]);
  });

  it("reads the workspace's widget look with its figures, and keeps it when the settings cannot be read", async () => {
    const widget = model();
    expect(widget.look.variant).toBe("rings");
    stubCollector([reading({})], false, null, "bars");
    await widget.refresh();
    expect(widget.look).toEqual({ ...DEFAULT_WIDGET_LOOK, variant: "bars" });
    stubCollector([reading({})], false, 503);
    await widget.refresh();
    expect(widget.look.variant).toBe("bars");
  });

  it("keeps a pick heard while a read was in flight over the read's older look", async () => {
    const widget = model();
    stubCollector([reading({})], false, null, "bars");
    const read = widget.refresh();
    widget.takeLook({ ...DEFAULT_WIDGET_LOOK, variant: "minimal" });
    await read;
    expect(widget.look.variant).toBe("minimal");
    await widget.refresh();
    expect(widget.look.variant).toBe("bars");
  });

  it("keeps the last figures when the collector cannot be reached", async () => {
    const widget = model();
    stubCollector([reading({})], false);
    await widget.refresh();
    stubCollector([], false, 503);
    await widget.refresh();
    expect(widget.isCollectorDown).toBe(true);
    expect(widget.cards).toHaveLength(1);
  });

  it("keeps the last figures and warns when a read fails some other way", async () => {
    const widget = model();
    stubCollector([reading({})], false);
    await widget.refresh();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    stubCollector([], false, 500);
    await widget.refresh();
    expect(widget.isCollectorDown).toBe(false);
    expect(widget.cards).toHaveLength(1);
    expect(warn).toHaveBeenCalledWith("[usage] could not read the widget's figures:", expect.anything());
    warn.mockRestore();
  });
});

describe("widgetRings", () => {
  it("gives each subscription its shortest window inside and its longest outside, with the detail to hover", () => {
    const cards = providerCards(
      [
        reading({}),
        reading({ ...WEEKLY, used_percentage: 34 }),
        reading({ source: "codex", used_percentage: 82, resets_at: "2026-10-22T21:00:00Z" }),
      ],
      [API_SPEND],
      NOW,
    );

    const rings = widgetRings(cards);

    expect(rings.map((pair) => [pair.name, pair.inner.usedPercent, pair.outer?.usedPercent ?? null])).toEqual([
      ["Claude", 61, 34],
      ["Codex", 82, null],
    ]);
    expect(rings[1].inner.level).toBe("high");
    expect(rings[0].tooltip.split(" · ")[0]).toBe("Claude");
    expect(rings[0].tooltip).toContain("Weekly limit: 34% used");
  });

  it("draws no arc for a window reset since its reading, and clamps the rest to a whole ring", () => {
    const [pair] = widgetRings(
      providerCards([reading({ used_percentage: 96, resets_at: "2026-10-22T19:00:00Z" })], [], NOW),
    );
    expect(ringFraction(pair.inner)).toBe(0);
    const [full] = widgetRings(providerCards([reading({ used_percentage: 130 })], [], NOW));
    expect(ringFraction(full.inner)).toBe(1);
    expect(widgetRings(providerCards([], [API_SPEND], NOW))).toEqual([]);
  });
});

describe("WidgetModel's stream and timers", () => {
  class FakeStream implements StreamLike {
    onopen: ((event: Event) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    isClosed = false;
    private readonly listeners: Record<string, ((event: MessageEvent<string>) => void)[]> = {};
    addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
      (this.listeners[type] ??= []).push(listener);
    }
    emit(type: string): void {
      for (const listener of this.listeners[type] ?? []) listener(new MessageEvent(type, { data: "{}" }));
    }
    close(): void {
      this.isClosed = true;
    }
  }

  let reads: string[];
  let streams: FakeStream[];
  let redraws: number;

  /** A started model over a collector that counts its limit reads (one per refresh). */
  function started(): WidgetModel {
    reads = [];
    streams = [];
    redraws = 0;
    vi.stubGlobal("fetch", async (url: string) => {
      reads.push(url);
      const answer = url.includes("/limits")
        ? { limits: [reading({})] }
        : url.includes("/totals")
          ? { rows: [] }
          : url.includes("/settings")
            ? {}
            : { status: "ok", catching_up: false };
      return new Response(JSON.stringify(answer), { status: 200 });
    });
    const widget = new WidgetModel({
      now: () => NOW,
      redraw: () => (redraws += 1),
      openStream: (url) => {
        expect(url).toBe(STREAM_URL);
        const stream = new FakeStream();
        streams.push(stream);
        return stream;
      },
    });
    widget.start();
    return widget;
  }

  const refreshes = (): number => reads.filter((url) => url.includes("/limits")).length;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("reads at once, again every minute, and is live once its stream opens", async () => {
    const widget = started();
    await vi.advanceTimersByTimeAsync(0);
    expect(refreshes()).toBe(1);
    expect(widget.isLive).toBe(false);

    const before = redraws;
    streams[0].onopen?.(new Event("open"));
    expect(widget.isLive).toBe(true);
    expect(redraws).toBe(before + 1);

    await vi.advanceTimersByTimeAsync(WIDGET_REFRESH_MS - 1);
    expect(refreshes()).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(refreshes()).toBe(2);
    widget.stop();
  });

  it("reads once shortly after a burst of calls on the stream, not once per call", async () => {
    const widget = started();
    await vi.advanceTimersByTimeAsync(0);
    streams[0].emit("model_call");
    streams[0].emit("model_call");
    await vi.advanceTimersByTimeAsync(EVENT_DEBOUNCE_MS - 1);
    streams[0].emit("model_call");
    expect(refreshes()).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(refreshes()).toBe(2);
    // A call after that read schedules the next one.
    streams[0].emit("model_call");
    await vi.advanceTimersByTimeAsync(EVENT_DEBOUNCE_MS);
    expect(refreshes()).toBe(3);
    widget.stop();
  });

  it("dims when its stream drops, and opens a new one after a backoff", async () => {
    const widget = started();
    streams[0].onopen?.(new Event("open"));
    const before = redraws;

    streams[0].onerror?.(new Event("error"));
    expect([widget.isLive, streams[0].isClosed, redraws]).toEqual([false, true, before + 1]);
    expect(streams).toHaveLength(1);

    // The first retry waits a second, give or take a fifth.
    await vi.advanceTimersByTimeAsync(1_200);
    expect(streams).toHaveLength(2);
    streams[1].onopen?.(new Event("open"));
    expect(widget.isLive).toBe(true);
    widget.stop();
  });

  it("stops reading and closes its stream when stopped, and ignores the closed stream's late error", async () => {
    const widget = started();
    await vi.advanceTimersByTimeAsync(0);
    streams[0].emit("model_call");
    widget.stop();
    expect(streams[0].isClosed).toBe(true);

    await vi.advanceTimersByTimeAsync(WIDGET_REFRESH_MS * 3);
    expect(refreshes()).toBe(1);

    const before = redraws;
    streams[0].onerror?.(new Event("error"));
    await vi.advanceTimersByTimeAsync(RECONNECT_CAP_MS);
    expect(streams).toHaveLength(1);
    expect(redraws).toBe(before);
  });
});
