/**
 * The page's state: which view the path names, the latest answer for it, whether the workspace answered, the
 * graphs' spans and the longer histories behind them, the open stop details, the walk of the disk, and the Stop
 * question.
 *
 * The page refreshes every few seconds on its own, so the header says nothing while that works and only speaks up
 * when a refresh fails. It polls only while it can be seen: a hidden or minimised window asks for nothing, and
 * catches up the moment it is shown again. While a disk walk runs, the disk view refreshes faster on one timer of
 * its own, so the interval's refreshes never start a second chain.
 */

import {
  fetchDisk,
  fetchHistory,
  fetchStats,
  startDiskScan,
  stopTarget,
  type DiskResponse,
  type HistorySample,
  type LongSpan,
  type Span,
  type StatsResponse,
  type StopTarget,
} from "./api";
import { STOP_WORDS } from "./format";

export type View = "overview" | "cpu" | "memory" | "disk";
export type ChartKind = "cpu" | "memory";

export const EVERY_MS = 5000;
export const SCANNING_EVERY_MS = 1500;
/** How long a longer span's samples are reused before they are read again. */
export const LONG_HISTORY_MAX_AGE_MS = 60_000;

const VIEWS: Record<string, View> = { "/": "overview", "/cpu": "cpu", "/memory": "memory", "/disk": "disk" };
export const VIEW_TITLES: Record<Exclude<View, "overview">, string> = { cpu: "CPU", memory: "Memory", disk: "Disk" };
export const APP_TITLE = "System Monitor";

export function viewOf(pathname: string): View {
  return VIEWS[pathname.replace(/\/+$/, "") || "/"] ?? "overview";
}

export function titleOf(view: View): string {
  return view === "overview" ? APP_TITLE : `${VIEW_TITLES[view]} · ${APP_TITLE}`;
}

/** A Stop button's question while it is up: asked, being carried out, or refused (with the reason). */
export interface StopQuestion {
  target: StopTarget;
  name: string;
  phase: "asking" | "stopping" | "failed";
  failure: string;
}

/** What the model needs of the page it runs in; the tests hand it their own. */
export interface MonitorHost {
  isHidden: () => boolean;
  onVisibilityChange: (listener: () => void) => void;
}

export const DOCUMENT_HOST: MonitorHost = {
  isHidden: () => document.hidden,
  onVisibilityChange: (listener) => document.addEventListener("visibilitychange", listener),
};

export class MonitorModel {
  readonly view: View;
  stats: StatsResponse | null = null;
  disk: DiskResponse | null = null;
  isOffline = false;
  spans: Record<ChartKind, Span> = { cpu: "10m", memory: "10m" };
  /** Which stops' Details are open, by when they happened, so a refresh never closes one. */
  readonly openStops = new Set<string>();
  /** A walk asked for whose request has not come back yet. */
  isScanRequested = false;
  stopQuestion: StopQuestion | null = null;
  private readonly longHistory: Partial<Record<LongSpan, { at: number; samples: HistorySample[] }>> = {};
  private timer: ReturnType<typeof setInterval> | null = null;
  private scanTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    pathname: string,
    private readonly redraw: () => void,
    private readonly host: MonitorHost = DOCUMENT_HOST,
  ) {
    this.view = viewOf(pathname);
  }

  /** Start polling while the page can be seen, and follow it being hidden and shown. */
  start(): void {
    this.host.onVisibilityChange(() => (this.host.isHidden() ? this.stopPolling() : this.startPolling()));
    if (!this.host.isHidden()) this.startPolling();
  }

  startPolling(): void {
    if (this.timer !== null) return;
    this.timer = setInterval(() => void this.refresh(), EVERY_MS);
    void this.refresh();
  }

  stopPolling(): void {
    if (this.timer !== null) clearInterval(this.timer);
    if (this.scanTimer !== null) clearTimeout(this.scanTimer);
    this.timer = null;
    this.scanTimer = null;
  }

  get isPolling(): boolean {
    return this.timer !== null;
  }

  async refresh(): Promise<void> {
    let isFailing = false;
    try {
      if (this.view === "disk") {
        this.disk = await fetchDisk();
      } else {
        this.stats = await fetchStats();
        await this.refreshLongHistory();
      }
    } catch {
      isFailing = true;
    }
    this.isOffline = isFailing;
    const isScanning = this.view === "disk" && this.disk?.scan.state === "scanning";
    if (isScanning && this.timer !== null && this.scanTimer === null) {
      this.scanTimer = setTimeout(() => {
        this.scanTimer = null;
        void this.refresh();
      }, SCANNING_EVERY_MS);
    }
    this.redraw();
  }

  /** The samples a graph draws: the 10-minute span rides with every reading, the longer ones come from the
   * history route. */
  samples(kind: ChartKind): HistorySample[] {
    const span = this.spans[kind];
    if (span === "10m") return this.stats?.history ?? [];
    return this.longHistory[span]?.samples ?? [];
  }

  async chooseSpan(kind: ChartKind, span: Span): Promise<void> {
    this.spans[kind] = span;
    this.redraw();
    if (span !== "10m") {
      await this.loadLongHistory(span);
      this.redraw();
    }
  }

  /** Read again the longer spans a graph shows whose samples are older than a minute. */
  private async refreshLongHistory(): Promise<void> {
    const kinds: ChartKind[] = this.view === "cpu" ? ["cpu"] : this.view === "memory" ? ["memory"] : [];
    for (const kind of kinds) {
      const span = this.spans[kind];
      if (span !== "10m") await this.loadLongHistory(span);
    }
  }

  private async loadLongHistory(span: LongSpan): Promise<void> {
    const kept = this.longHistory[span];
    if (kept !== undefined && Date.now() - kept.at <= LONG_HISTORY_MAX_AGE_MS) return;
    try {
      this.longHistory[span] = { at: Date.now(), samples: await fetchHistory(span) };
    } catch {
      // The graph keeps what it had; the next reading tries again.
    }
  }

  toggleStopDetails(at: string): void {
    if (this.openStops.has(at)) this.openStops.delete(at);
    else this.openStops.add(at);
  }

  async scanDisk(): Promise<void> {
    this.isScanRequested = true;
    this.redraw();
    try {
      await startDiskScan();
    } catch {
      // The refresh below shows the walk's state either way.
    }
    this.isScanRequested = false;
    await this.refresh();
  }

  askToStop(target: StopTarget, name: string): void {
    this.stopQuestion = { target, name, phase: "asking", failure: "" };
  }

  dismissStop(): void {
    if (this.stopQuestion?.phase === "stopping") return;
    this.stopQuestion = null;
  }

  /** Carry out the question's stop: the next refresh shows the row gone, or the refusal stays up until dismissed. */
  async confirmStop(): Promise<void> {
    const question = this.stopQuestion;
    if (question === null || question.phase !== "asking") return;
    question.phase = "stopping";
    this.redraw();
    const failure = await stopTarget(question.target, STOP_WORDS[question.target.kind].verb);
    if (failure !== "") {
      question.phase = "failed";
      question.failure = failure;
      this.redraw();
      return;
    }
    this.stopQuestion = null;
    await this.refresh();
  }
}
