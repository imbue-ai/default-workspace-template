/**
 * The page's state: the chosen window, breakdown and filter, what the collector last answered for them, the
 * latest turns, and the live stream that keeps them current.
 *
 * The page never adds up streamed calls itself: a new call schedules a refetch of the totals shortly after
 * (debounced, so a burst costs one), and of the first page of turns too. The live windows refetch on
 * every new call; the long ones at most every SLOW_REFRESH_MS. A timer refetches every PERIODIC_REFRESH_MS too,
 * so a rolling window keeps moving with no calls at all.
 */

import { ReconnectBackoff } from "@imbue/workspace-ui/src/models/backoff";

import {
  type LatencyRow,
  type ModelCall,
  type StoredCall,
  type TurnRow,
  type TotalsRow,
  fetchAppNames,
  fetchChatAndRoutineTitles,
  fetchCalls,
  fetchTurnCount,
  fetchTurns,
  fetchCollectorHealth,
  fetchLatency,
  fetchLimits,
  fetchTotals,
  isUnreachable,
  turnFilter,
  turnKey,
} from "./api";
import {
  type BreakdownId,
  type Filter,
  NO_SOURCE_NAMES,
  type SourceNames,
  breakdownSpec,
  callGroup,
  filterKey,
  matchesFilter,
  withFilterFields,
} from "./breakdown";
import { DEFAULT_ROWS_PER_PAGE, type RowsPerPage, pageCount, pageHolding, pageOffset } from "./callPages";
import { LIMITS_LOOKBACK_MS, type ProviderCard, providerCards } from "./providers";
import { type Route, combinedFilters, inScope, scopeFilters } from "./routing";
import { SECTION_FIELDS, type SectionSpec, detailSections, inSection } from "./sections";
import { type Summary, failedShare, summarize } from "./summary";
import { type UsageScale, calibrationSpan, limitWindowStart, percentPerDollar } from "./usageEstimate";
import { type WindowId, windowSpec, windowStart } from "./windows";

export const EVENT_DEBOUNCE_MS = 2_000;
export const SLOW_REFRESH_MS = 30_000;
export const PERIODIC_REFRESH_MS = 60_000;
/**
 * How long the collector may stay unreachable, or the live stream down, before the page says so. The collector is
 * almost always up, and what breaks the line is usually brief (a restart, or the tunnel to the workspace answering
 * 502-504 for a moment), so until then reads are retried quietly and the page keeps its numbers as they are.
 */
export const OUTAGE_GRACE_MS = 60_000;
// More calls than one turn makes in practice; the collector answers up to 500.
const TURN_CALLS_LIMIT = 500;

export type ChartMetric = "spend" | "tokens" | "usage";
/** What a section's chart splits its usage by. */
export type ChartSeries = "source" | "model";

/** The tiles' numbers for some set of calls: the Total row's, or one provider section's. */
export interface UsageFigures {
  summary: Summary;
  failed: { calls: number; failed: number };
  /** How many turns those calls belong to. */
  turns: number;
  latency: LatencyRow | null;
}

/** A set of calls' tiles, chart rows (bucketed, by model and by source) and breakdown: all usage, or one provider's. */
export interface SectionFigures extends UsageFigures {
  chartRows: TotalsRow[];
  sourceChartRows: TotalsRow[];
  breakdownRows: TotalsRow[];
  breakdownLatency: LatencyRow[];
  /** For a subscription: its breakdown rows within each limit's most recent window, by window key. */
  limitWindowRows?: Record<string, TotalsRow[]>;
}

/** One provider's details. */
export interface DetailSection extends SectionFigures {
  spec: SectionSpec;
}

export interface DashboardData {
  total: SectionFigures;
  sections: DetailSection[];
  windowId: WindowId;
  breakdown: BreakdownId;
  loadedAt: Date;
}

export interface StreamLike {
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  close(): void;
}

export interface DashboardDeps {
  /** The page shown, which scopes the latest calls to its item. */
  route: Route;
  now: () => Date;
  redraw: () => void;
  openStream: (url: string) => StreamLike;
}

export const STREAM_URL = "/api/usage/stream";

export class DashboardModel {
  windowId: WindowId = "24h";
  breakdown: BreakdownId = "source";
  filter: Filter | null = null;
  /** The chart each section shows, once its viewer picked one; a section otherwise shows its default. */
  chartMetrics: Record<string, ChartMetric> = {};
  /** What each section's chart is split by, once its viewer picked; a section otherwise shows it by source. */
  chartSeries: Record<string, ChartSeries> = {};
  /** Each subscription's usage scales, by its section key: one per limit window that can be estimated. */
  usageScales: Record<string, UsageScale[]> = {};
  /** The limit window each subscription's usage chart is against, once its viewer picked one. */
  usageWindows: Record<string, string> = {};
  /** The cards at the top, which answer for now and this month whatever the Details window or filter. */
  providers: ProviderCard[] | null = null;
  data: DashboardData | null = null;
  turns: TurnRow[] = [];
  /** How many turns the page's item and filter have, over every page. */
  turnsTotal = 0;
  /** The page of turns shown, from 1. */
  turnsPage = 1;
  turnsPerPage: RowsPerPage = DEFAULT_ROWS_PER_PAGE;
  /** The turn opened to its calls, by its key, and those calls once loaded. */
  expandedTurnKey: string | null = null;
  expandedTurnCalls: StoredCall[] | null = null;
  sourceNames: SourceNames = NO_SOURCE_NAMES;
  isLoading = false;
  isCollectorDown = false;
  isCatchingUp = false;
  isLive = false;
  errorMessage: string | null = null;
  expandedCallId: string | null = null;

  private generation = 0;
  private turnsGeneration = 0;
  private turnsReload: ReturnType<typeof setTimeout> | null = null;
  private lastRefreshAt = 0;
  private stream: StreamLike | null = null;
  private wasDisconnected = false;
  private readonly streamBackoff = new ReconnectBackoff();
  private readonly retryBackoff = new ReconnectBackoff();
  private eventRefresh: ReturnType<typeof setTimeout> | null = null;
  private retry: ReturnType<typeof setTimeout> | null = null;
  private periodic: ReturnType<typeof setInterval> | null = null;
  /** When reads began failing to reach the collector, while they still are. */
  private unreachableSince: number | null = null;
  /** Shows the stream as down once it has stayed down for the grace period. */
  private streamDownTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly deps: DashboardDeps) {}

  start(): void {
    void this.loadSourceNames();
    void this.refresh();
    void this.loadTurns();
    this.connectStream();
    this.periodic = setInterval(() => {
      void this.refresh();
      // Chats are made and renamed while the page is open.
      void this.loadSourceNames();
    }, PERIODIC_REFRESH_MS);
  }

  /** The names sources are shown under; a lookup that fails leaves the names already known. */
  async loadSourceNames(): Promise<void> {
    const [apps, titles] = await Promise.all([
      fetchAppNames().catch(() => this.sourceNames.apps),
      fetchChatAndRoutineTitles().catch(() => this.sourceNames),
    ]);
    this.sourceNames = { apps, chats: titles.chats, routines: titles.routines };
    this.deps.redraw();
  }

  stop(): void {
    this.stream?.close();
    this.stream = null;
    for (const timer of [this.eventRefresh, this.retry, this.turnsReload, this.streamDownTimer]) {
      if (timer !== null) clearTimeout(timer);
    }
    if (this.periodic !== null) clearInterval(this.periodic);
  }

  selectWindow(windowId: WindowId): void {
    this.windowId = windowId;
    void this.refresh();
  }

  selectBreakdown(breakdown: BreakdownId): void {
    this.breakdown = breakdown;
    void this.refresh();
  }

  selectUsageWindow(sectionKey: string, windowKey: string): void {
    this.usageWindows = { ...this.usageWindows, [sectionKey]: windowKey };
    this.deps.redraw();
  }

  selectChartMetric(sectionKey: string, metric: ChartMetric): void {
    this.chartMetrics = { ...this.chartMetrics, [sectionKey]: metric };
    this.deps.redraw();
  }

  selectChartSeries(sectionKey: string, series: ChartSeries): void {
    this.chartSeries = { ...this.chartSeries, [sectionKey]: series };
    this.deps.redraw();
  }

  /** Filter the page to ``filter``, or clear the filter when it is the one already set. */
  toggleFilter(filter: Filter | null): void {
    this.filter =
      filter !== null && this.filter !== null && filterKey(filter) === filterKey(this.filter) ? null : filter;
    void this.refresh();
    this.showTurnsPage(1);
  }

  toggleCall(eventId: string): void {
    this.expandedCallId = this.expandedCallId === eventId ? null : eventId;
    this.deps.redraw();
  }

  /** Fetch everything the tiles, the chart and the breakdown show for the current window, breakdown and filter. */
  async refresh(): Promise<void> {
    const generation = ++this.generation;
    const { windowId, breakdown, filter } = this;
    const now = this.deps.now();
    const since = windowStart(windowId, now);
    const bucket = windowSpec(windowId).bucket;
    const filterFields = withFilterFields([], filter);
    const breakdownFields = withFilterFields(breakdownSpec(breakdown).fields, filter);
    this.isLoading = true;
    this.lastRefreshAt = now.getTime();
    this.deps.redraw();
    try {
      const split = (fields: readonly string[]): string[] => withFilterFields([...fields, ...SECTION_FIELDS], filter);
      const [
        limits,
        monthRows,
        summaryRows,
        statusRows,
        chartRows,
        sourceChartRows,
        breakdownRows,
        totalLatency,
        totalBreakdownLatency,
        totalTurns,
        health,
      ] = await Promise.all([
        fetchLimits(new Date(now.getTime() - LIMITS_LOOKBACK_MS)),
        fetchTotals({ groupBy: ["provider", "billing_mode", "cost_provenance"], since: windowStart("month", now) }),
        fetchTotals({ groupBy: split(["cost_provenance"]), since }),
        fetchTotals({ groupBy: split(["status"]), since }),
        fetchTotals({ groupBy: split(["model"]), since, bucket }),
        fetchTotals({ groupBy: split(["origin_kind", "origin_id"]), since, bucket }),
        fetchTotals({ groupBy: split(breakdownSpec(breakdown).fields), since }),
        fetchLatency(filterFields, since),
        fetchLatency(breakdownFields, since),
        fetchTurnCount(combinedFilters({}, filter?.values ?? null), since),
        fetchCollectorHealth(),
      ]);
      if (generation !== this.generation) return;
      const keep = <T extends { group: Record<string, string | null> }>(rows: T[]): T[] =>
        rows.filter((row) => matchesFilter(row.group, filter));
      const specs = detailSections(keep(summaryRows));
      const cards = providerCards(limits, monthRows, now);
      const calibrations = cards.flatMap((card) =>
        card.kind !== "subscription"
          ? []
          : card.windows.flatMap((window) => {
              const span = calibrationSpan(window);
              const start = limitWindowStart(window);
              return span === null || start === null || window.readingResetsAt === null
                ? []
                : [{ card, window, span, start, resetsAt: window.readingResetsAt }];
            }),
      );
      const [sectionReads, calibrationRows, limitWindowRows] = await Promise.all([
        Promise.all(
          specs.map((spec) => {
            const only = { provider: [spec.provider], billing_mode: spec.billingModes };
            return Promise.all([
              fetchLatency(filterFields, since, only),
              fetchLatency(breakdownFields, since, only),
              fetchTurnCount(combinedFilters(only, filter?.values ?? null), since),
            ]);
          }),
        ),
        Promise.all(
          calibrations.map(({ span }) =>
            fetchTotals({ groupBy: ["provider", "billing_mode"], since: span.since, until: span.until }),
          ),
        ),
        // Each limit's most recent window, up to its reset or now, for the breakdown's per-limit columns.
        Promise.all(
          calibrations.map(({ start, resetsAt }) =>
            fetchTotals({
              groupBy: withFilterFields([...breakdownSpec(breakdown).fields, ...SECTION_FIELDS], filter),
              since: start,
              until: resetsAt < now ? resetsAt : now,
            }),
          ),
        ),
      ]);
      if (generation !== this.generation) return;
      const within = <T extends { group: Record<string, string | null> }>(rows: T[], spec: SectionSpec): T[] =>
        keep(rows).filter((row) => inSection(row.group, spec));
      this.providers = cards;
      const scales: Record<string, UsageScale[]> = {};
      calibrations.forEach(({ card, window, span, start, resetsAt }, index) => {
        const provider = card.sectionKey.slice("subscription:".length);
        const listPrice = calibrationRows[index]
          .filter((row) => row.group.provider === provider && row.group.billing_mode === "SUBSCRIPTION")
          .reduce((sum, row) => sum + (row.cost_usd === null ? 0 : Number(row.cost_usd)), 0);
        const rate = percentPerDollar(span.rise, listPrice);
        if (rate === null) return;
        scales[card.sectionKey] = [
          ...(scales[card.sectionKey] ?? []),
          {
            windowKey: window.key,
            label: window.label,
            percentPerDollar: rate,
            isFromEarlierWindow: window.isResetSinceReading,
            windowStart: start,
            resetsAt,
          },
        ];
      });
      this.usageScales = scales;
      this.data = {
        total: {
          summary: summarize(keep(summaryRows)),
          failed: failedShare(keep(statusRows)),
          turns: totalTurns,
          latency: keep(totalLatency)[0] ?? null,
          chartRows: keep(chartRows),
          sourceChartRows: keep(sourceChartRows),
          breakdownRows: keep(breakdownRows),
          breakdownLatency: keep(totalBreakdownLatency),
        },
        sections: specs.map((spec, index) => ({
          spec,
          summary: summarize(within(summaryRows, spec)),
          failed: failedShare(within(statusRows, spec)),
          turns: sectionReads[index][2],
          latency: keep(sectionReads[index][0])[0] ?? null,
          chartRows: within(chartRows, spec),
          sourceChartRows: within(sourceChartRows, spec),
          breakdownRows: within(breakdownRows, spec),
          breakdownLatency: keep(sectionReads[index][1]),
          limitWindowRows: Object.fromEntries(
            calibrations.flatMap(({ card, window }, calibration) =>
              card.sectionKey === spec.key ? [[window.key, within(limitWindowRows[calibration], spec)]] : [],
            ),
          ),
        })),
        windowId,
        breakdown,
        loadedAt: now,
      };
      this.isCatchingUp = health.catching_up;
      this.isCollectorDown = false;
      this.unreachableSince = null;
      this.errorMessage = null;
      this.retryBackoff.reset();
    } catch (error) {
      if (generation !== this.generation) return;
      this.noteFailure(error, () => void this.refresh());
    } finally {
      if (generation === this.generation) {
        this.isLoading = false;
        this.deps.redraw();
      }
    }
  }

  /** Load the page of turns shown, for the current filter, and the opened turn's calls if it is still there. */
  async loadTurns(): Promise<void> {
    const generation = ++this.turnsGeneration;
    try {
      const answer = await fetchTurns({
        filters: this.callFilters(this.filter),
        limit: this.turnsPerPage,
        offset: pageOffset(this.turnsPage, this.turnsPerPage),
      });
      if (generation !== this.turnsGeneration) return;
      this.turns = answer.turns;
      this.turnsTotal = answer.total;
      // Turns can leave between asks only through a rebuilt index, which can leave the page past the last one.
      const count = pageCount(answer.total, this.turnsPerPage);
      if (this.turnsPage > count) {
        this.showTurnsPage(count);
        return;
      }
      const expanded = this.turns.find((turn) => turnKey(turn) === this.expandedTurnKey);
      if (expanded !== undefined) void this.loadTurnCalls(expanded);
    } catch (error) {
      this.noteFailure(error, () => void this.loadTurns());
    }
    this.deps.redraw();
  }

  /** Show ``perPage`` turns to a page, on the page holding the first turn shown before. */
  selectTurnsPerPage(perPage: RowsPerPage): void {
    const page = pageHolding(pageOffset(this.turnsPage, this.turnsPerPage), perPage);
    this.turnsPerPage = perPage;
    this.showTurnsPage(page);
  }

  showTurnsPage(page: number): void {
    this.turnsPage = page;
    this.collapseTurn();
    void this.loadTurns();
  }

  /** Open a turn to its calls, or close it when it is the one open. */
  toggleTurn(turn: TurnRow): void {
    if (this.expandedTurnKey === turnKey(turn)) {
      this.collapseTurn();
    } else {
      this.expandedTurnKey = turnKey(turn);
      this.expandedTurnCalls = null;
      this.expandedCallId = null;
      void this.loadTurnCalls(turn);
    }
    this.deps.redraw();
  }

  private collapseTurn(): void {
    this.expandedTurnKey = null;
    this.expandedTurnCalls = null;
    this.expandedCallId = null;
  }

  /** One turn's calls within the page's scope, newest first. */
  private async loadTurnCalls(turn: TurnRow): Promise<void> {
    const key = turnKey(turn);
    try {
      const answer = await fetchCalls({
        filters: { ...this.callFilters(this.filter), ...turnFilter(turn) },
        limit: TURN_CALLS_LIMIT,
      });
      if (this.expandedTurnKey !== key) return;
      this.expandedTurnCalls = answer.calls;
    } catch (error) {
      this.noteFailure(error, () => undefined);
    }
    this.deps.redraw();
  }

  /** Take one streamed call: schedule a refetch of the first page of turns if it belongs there, and of the totals. */
  receiveCall(call: ModelCall): void {
    const group = callGroup(call);
    // Only the first page takes new turns in; a later one keeps the rows being read.
    if (matchesFilter(group, this.filter) && inScope(group, this.deps.route) && this.turnsPage === 1) {
      this.scheduleTurnsReload();
    }
    this.scheduleEventRefresh();
    this.deps.redraw();
  }

  /** The calls filters for the page's item, narrowed by ``filter``. */
  private callFilters(filter: Filter | null): Record<string, readonly string[]> {
    return combinedFilters(scopeFilters(this.deps.route), filter?.values ?? null);
  }

  /**
   * Record why a read failed: a collector that cannot be reached (retried with backoff, and called down only once it
   * has stayed unreachable for the grace period), or anything else.
   */
  private noteFailure(error: unknown, retry: () => void): void {
    if (isUnreachable(error)) {
      const now = this.deps.now().getTime();
      this.unreachableSince ??= now;
      this.isCollectorDown = now - this.unreachableSince >= OUTAGE_GRACE_MS;
      if (this.retry === null) {
        this.retry = setTimeout(() => {
          this.retry = null;
          retry();
        }, this.retryBackoff.nextDelay());
      }
      return;
    }
    this.errorMessage = error instanceof Error ? error.message : String(error);
  }

  /** Reload the turns shortly, so a burst of calls (one turn's, typically) costs one reload. */
  private scheduleTurnsReload(): void {
    if (this.turnsReload !== null) return;
    this.turnsReload = setTimeout(() => {
      this.turnsReload = null;
      void this.loadTurns();
    }, EVENT_DEBOUNCE_MS);
  }

  private scheduleEventRefresh(): void {
    if (this.eventRefresh !== null) return;
    const sinceLast = this.deps.now().getTime() - this.lastRefreshAt;
    const wait = windowSpec(this.windowId).isLive
      ? EVENT_DEBOUNCE_MS
      : Math.max(EVENT_DEBOUNCE_MS, SLOW_REFRESH_MS - sinceLast);
    this.eventRefresh = setTimeout(() => {
      this.eventRefresh = null;
      void this.refresh();
    }, wait);
  }

  private connectStream(): void {
    const stream = this.deps.openStream(STREAM_URL);
    this.stream = stream;
    stream.onopen = () => {
      if (this.streamDownTimer !== null) clearTimeout(this.streamDownTimer);
      this.streamDownTimer = null;
      this.isLive = true;
      this.streamBackoff.reset();
      if (this.wasDisconnected) {
        this.wasDisconnected = false;
        this.fillGap();
      }
      this.deps.redraw();
    };
    stream.addEventListener("model_call", (event) => {
      this.receiveCall(JSON.parse(event.data) as ModelCall);
    });
    stream.onerror = () => {
      stream.close();
      if (this.stream !== stream) return;
      this.stream = null;
      this.wasDisconnected = true;
      // "Live" stays up through a brief drop; the gap is filled from the collector once the stream is back.
      if (this.isLive && this.streamDownTimer === null) {
        this.streamDownTimer = setTimeout(() => {
          this.streamDownTimer = null;
          this.isLive = false;
          this.deps.redraw();
        }, OUTAGE_GRACE_MS);
      }
      setTimeout(() => this.connectStream(), this.streamBackoff.nextDelay());
    };
  }

  /** After a reconnect: the page of turns again, with any calls stored while the stream was down, then fresh totals. */
  private fillGap(): void {
    void this.loadTurns();
    void this.refresh();
  }
}
