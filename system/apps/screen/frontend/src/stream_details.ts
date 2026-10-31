/** What the media path is, for the strip's Details panel and the console: the selected ICE pair (direct or relayed,
 *  udp or tcp, the remote address), the picture's size and rate, the round trip and the bitrate, read from the peer's
 *  ``getStats()``; or the screencast, when no pair connected. Summarizing is pure over the stats entries so a test can
 *  feed it a report by hand. */

import type { ConnectInfo } from "./state";

/** The entries of an ``RTCStatsReport`` the summary reads, as plain objects. */
export interface StatsEntry {
  id: string;
  type: string;
  [key: string]: unknown;
}

export interface BytesSample {
  bytesReceived: number;
  timestampMs: number;
}

export interface PathSummary {
  /** "direct", "relay", or "none" while no pair is selected. */
  path: "direct" | "relay" | "none";
  protocol: string | null;
  remoteAddress: string | null;
  roundTripMs: number | null;
  frameWidth: number | null;
  frameHeight: number | null;
  framesPerSecond: number | null;
  kilobitsPerSecond: number | null;
  sample: BytesSample | null;
}

const EMPTY_SUMMARY: PathSummary = {
  path: "none",
  protocol: null,
  remoteAddress: null,
  roundTripMs: null,
  frameWidth: null,
  frameHeight: null,
  framesPerSecond: null,
  kilobitsPerSecond: null,
  sample: null,
};

function numberOf(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringOf(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** The selected pair and the video's inbound stream, from one report; ``previous`` is the last sample of bytes
 *  received, for the bitrate since then. */
export function summarizeStats(entries: readonly StatsEntry[], previous: BytesSample | null): PathSummary {
  const byId = new Map(entries.map((entry) => [entry.id, entry]));
  const transport = entries.find((entry) => entry.type === "transport" && stringOf(entry.selectedCandidatePairId));
  const pairId = transport ? stringOf(transport.selectedCandidatePairId) : null;
  const pair =
    (pairId ? byId.get(pairId) : undefined) ??
    entries.find(
      (entry) => entry.type === "candidate-pair" && entry.nominated === true && entry.state === "succeeded",
    );
  const inbound = entries.find((entry) => entry.type === "inbound-rtp" && entry.kind === "video");
  let summary: PathSummary = { ...EMPTY_SUMMARY };
  if (pair) {
    const local = byId.get(String(pair.localCandidateId ?? ""));
    const remote = byId.get(String(pair.remoteCandidateId ?? ""));
    const isRelay = local?.candidateType === "relay" || remote?.candidateType === "relay";
    const protocol = stringOf(local?.relayProtocol) ?? stringOf(remote?.protocol) ?? stringOf(local?.protocol);
    const address = stringOf(remote?.address) ?? stringOf(remote?.ip);
    const port = numberOf(remote?.port);
    const roundTrip = numberOf(pair.currentRoundTripTime);
    summary = {
      ...summary,
      path: isRelay ? "relay" : "direct",
      protocol: protocol ? protocol.toLowerCase() : null,
      remoteAddress: address ? (port !== null ? `${address}:${port}` : address) : null,
      roundTripMs: roundTrip !== null ? Math.round(roundTrip * 1000) : null,
    };
  }
  if (inbound) {
    const bytes = numberOf(inbound.bytesReceived);
    const timestamp = numberOf(inbound.timestamp);
    const sample = bytes !== null && timestamp !== null ? { bytesReceived: bytes, timestampMs: timestamp } : null;
    let kilobitsPerSecond: number | null = null;
    if (sample && previous && sample.timestampMs > previous.timestampMs) {
      const seconds = (sample.timestampMs - previous.timestampMs) / 1000;
      kilobitsPerSecond = Math.round(((sample.bytesReceived - previous.bytesReceived) * 8) / 1000 / seconds);
    }
    summary = {
      ...summary,
      frameWidth: numberOf(inbound.frameWidth),
      frameHeight: numberOf(inbound.frameHeight),
      framesPerSecond: numberOf(inbound.framesPerSecond),
      kilobitsPerSecond,
      sample,
    };
  }
  return summary;
}

/** One line naming the path, for the console and the panel's first row. */
export function describePath(summary: PathSummary): string {
  if (summary.path === "none") return "No media path connected";
  const how = summary.path === "relay" ? "through a relay (TURN)" : "direct";
  const protocol = summary.protocol ? ` over ${summary.protocol}` : "";
  const where = summary.remoteAddress ? ` to ${summary.remoteAddress}` : "";
  return `WebRTC ${how}${protocol}${where}`;
}

export interface DetailsContext {
  /** The viewer's stream status, as the strip shows it. */
  isScreencast: boolean;
  /** The profile the service announced (``idle``, ``watch``, ``control``). */
  videoId: string;
  screenSize: { width: number; height: number } | null;
  /** What ``connect-info`` told the viewer last, so the panel says which endpoints were tried. */
  connectInfo: ConnectInfo;
  isRelayAttempt: boolean;
  screencastFps: number;
}

/** The panel's rows: path, endpoints, picture, rate. */
export function describeDetails(summary: PathSummary, context: DetailsContext): string[] {
  const rows: string[] = [];
  if (context.isScreencast) {
    rows.push(`Path: JPEG screencast at ${context.screencastFps} fps (no WebRTC pair connected; retrying)`);
  } else {
    rows.push(`Path: ${describePath(summary)}`);
  }
  const endpoints = context.connectInfo.candidates.map(
    (candidate) => `${candidate.protocol} ${candidate.host}:${candidate.port}`,
  );
  rows.push(`Endpoints offered: ${endpoints.length > 0 ? endpoints.join(", ") : "none (the server's own address)"}`);
  rows.push(
    `Relay servers: ${context.connectInfo.ice_servers.length}${context.isRelayAttempt ? " (relay attempt)" : ""}`,
  );
  const size = context.screenSize ? `${context.screenSize.width}x${context.screenSize.height}` : "unknown";
  rows.push(`Screen: ${size}, profile ${context.videoId}`);
  if (!context.isScreencast && summary.path !== "none") {
    const picture =
      summary.frameWidth !== null && summary.frameHeight !== null
        ? `${summary.frameWidth}x${summary.frameHeight}`
        : "no frames yet";
    const fps = summary.framesPerSecond !== null ? `, ${summary.framesPerSecond} fps` : "";
    const rate = summary.kilobitsPerSecond !== null ? `, ${summary.kilobitsPerSecond} kbit/s` : "";
    const rtt = summary.roundTripMs !== null ? `, round trip ${summary.roundTripMs} ms` : "";
    rows.push(`Picture: ${picture}${fps}${rate}${rtt}`);
  }
  return rows;
}

/** Turns an ``RTCStatsReport`` into the plain entries the summary reads. */
export function entriesOf(report: RTCStatsReport): StatsEntry[] {
  const entries: StatsEntry[] = [];
  report.forEach((value: unknown) => {
    if (value !== null && typeof value === "object" && "id" in value && "type" in value) {
      entries.push(value as StatsEntry);
    }
  });
  return entries;
}

const REFRESH_MS = 1000;

export interface DetailsSource {
  /** The peer's statistics, or null without a peer. */
  getStats(): Promise<RTCStatsReport> | null;
  context(): DetailsContext;
}

/** The panel under the strip: the rows above, refreshed every second while open. */
export class DetailsPanel {
  readonly element = document.createElement("div");
  private readonly list = document.createElement("ul");
  private timer = 0;
  private lastSample: BytesSample | null = null;
  /** Bumped by every open and close, so a refresh still awaiting its stats when the panel was toggled stops instead
   *  of running on beside the one the reopen started. */
  private generation = 0;

  constructor(private readonly source: DetailsSource) {
    this.element.className = "screen-details";
    this.element.dataset.screenDetailsPanel = "true";
    this.element.hidden = true;
    this.element.append(this.list);
  }

  setOpen(isOpen: boolean): void {
    this.generation += 1;
    this.element.hidden = !isOpen;
    window.clearTimeout(this.timer);
    this.timer = 0;
    this.lastSample = null;
    if (isOpen) void this.refresh(this.generation);
  }

  private async refresh(generation: number): Promise<void> {
    const summary = await this.summarize();
    if (generation !== this.generation) return;
    this.lastSample = summary.sample;
    const rows = describeDetails(summary, this.source.context());
    this.list.replaceChildren(
      ...rows.map((row) => {
        const item = document.createElement("li");
        item.textContent = row;
        return item;
      }),
    );
    this.timer = window.setTimeout(() => void this.refresh(generation), REFRESH_MS);
  }

  private async summarize(): Promise<PathSummary> {
    const stats = this.source.getStats();
    if (stats === null) return summarizeStats([], null);
    try {
      return summarizeStats(entriesOf(await stats), this.lastSample);
    } catch (error) {
      console.warn("[screen] stats failed", error);
      return summarizeStats([], null);
    }
  }
}
