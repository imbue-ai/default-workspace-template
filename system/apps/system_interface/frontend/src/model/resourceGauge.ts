/**
 * The tray's resource gauge, as the shell reports it: the user's settings, how much room the workspace has left
 * (disk, memory, CPU), the warnings raised, and the programs the memory guard stopped lately. The shell takes the
 * readings and judges the warnings; it pushes the gauge over the socket (`resource_gauge_changed`, on connect, when
 * what the tray draws changes, and once a minute regardless), so the tray never polls. The card, which shows exact
 * figures, fetches the gauge while it is open.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { postJson, requestJson } from "@imbue/workspace-ui/src/models/http";

export type GaugeResource = "cpu" | "memory" | "disk";
export type GaugeVisibility = "always" | "when_warning";
export type AlertSeverity = "warning" | "critical";

/** The resources in the order the gauge draws them. */
export const GAUGE_RESOURCES: readonly GaugeResource[] = ["cpu", "memory", "disk"];
export const MIN_WARNING_PERCENT = 50;
export const MAX_WARNING_PERCENT = 99;
export const MIN_DISK_WARNING_FREE_GB = 0.5;
export const MAX_DISK_WARNING_FREE_GB = 500;
/** The shell pushes at least once a minute; a gauge silent for this long is no longer being updated. */
export const STALE_AFTER_MS = 150_000;
/** How often the open card fetches the gauge. */
export const CARD_REFRESH_INTERVAL_MS = 5000;

export const SYSTEM_MONITOR_APP_NAME = "system-monitor";
/** The System Monitor's pages: a window on any of them is pointed at the view asked for rather than another opened. */
export const SYSTEM_MONITOR_PAGES: readonly string[] = ["/", "/cpu", "/memory", "/disk"];

/** The System Monitor view about a resource. */
export function systemMonitorPathOf(resource: GaugeResource): string {
  return `/${resource}`;
}

export interface ResourceGaugeSettings {
  readonly visibility: GaugeVisibility;
  readonly diskWarningFreeGb: number | null;
  readonly memoryWarningPercent: number | null;
  readonly cpuWarningPercent: number | null;
}

export interface AmountReading {
  readonly usedBytes: number;
  readonly freeBytes: number;
  readonly totalBytes: number;
}

export interface DiskReading extends AmountReading {
  /** How long until the disk is full at the rate it has been filling; null when it is not, or not known. */
  readonly secondsToFull: number | null;
}

export interface CpuReading {
  readonly percent: number;
  readonly recentPercent: number;
  readonly cores: number;
}

export interface ResourceReadings {
  readonly cpu: CpuReading | null;
  readonly memory: AmountReading | null;
  readonly disk: DiskReading | null;
}

export interface ResourceAlert {
  readonly resource: GaugeResource;
  readonly severity: AlertSeverity;
  /** A few words naming the problem. */
  readonly headline: string;
  /** The figures behind it. */
  readonly detail: string;
}

export interface StoppedProgram {
  /** ISO 8601. */
  readonly at: string;
  readonly what: string;
}

export interface ResourceGaugeView {
  readonly settings: ResourceGaugeSettings;
  readonly readings: ResourceReadings;
  /** The raised warnings, the most severe first. */
  readonly alerts: readonly ResourceAlert[];
  /** The programs the memory guard stopped in the last day, newest first. */
  readonly recentStops: readonly StoppedProgram[];
}

/** How a resource's bar is drawn. */
export type GaugeLevel = "normal" | "warning" | "critical" | "unknown";

function asRecord(raw: unknown): Record<string, unknown> {
  return raw !== null && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
}

function asNumberOrNull(raw: unknown): number | null {
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

function amountFromWire(raw: unknown): AmountReading | null {
  const record = asRecord(raw);
  const used = asNumberOrNull(record.used_bytes);
  const free = asNumberOrNull(record.free_bytes);
  const total = asNumberOrNull(record.total_bytes);
  if (used === null || free === null || total === null || total <= 0) return null;
  return { usedBytes: used, freeBytes: free, totalBytes: total };
}

function cpuFromWire(raw: unknown): CpuReading | null {
  const record = asRecord(raw);
  const percent = asNumberOrNull(record.percent);
  if (percent === null) return null;
  return {
    percent,
    recentPercent: asNumberOrNull(record.recent_percent) ?? percent,
    cores: asNumberOrNull(record.cores) ?? 1,
  };
}

const RESOURCE_SET: ReadonlySet<string> = new Set(GAUGE_RESOURCES);

function alertsFromWire(raw: unknown): ResourceAlert[] {
  if (!Array.isArray(raw)) return [];
  const alerts: ResourceAlert[] = [];
  for (const entry of raw) {
    const record = asRecord(entry);
    if (typeof record.resource !== "string" || !RESOURCE_SET.has(record.resource)) continue;
    alerts.push({
      resource: record.resource as GaugeResource,
      severity: record.severity === "critical" ? "critical" : "warning",
      headline: typeof record.headline === "string" ? record.headline : "",
      detail: typeof record.detail === "string" ? record.detail : "",
    });
  }
  return alerts;
}

function stopsFromWire(raw: unknown): StoppedProgram[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map(asRecord)
    .filter((record) => typeof record.at === "string" && typeof record.what === "string")
    .map((record) => ({ at: record.at as string, what: record.what as string }));
}

/** The gauge as the shell sends it, with anything unreadable left out rather than thrown on. */
export function viewFromWire(raw: unknown): ResourceGaugeView {
  const record = asRecord(raw);
  const settings = asRecord(record.settings);
  const readings = asRecord(record.readings);
  const disk = amountFromWire(readings.disk);
  return {
    settings: {
      visibility: settings.visibility === "always" ? "always" : "when_warning",
      diskWarningFreeGb: asNumberOrNull(settings.disk_warning_free_gb),
      memoryWarningPercent: asNumberOrNull(settings.memory_warning_percent),
      cpuWarningPercent: asNumberOrNull(settings.cpu_warning_percent),
    },
    readings: {
      cpu: cpuFromWire(readings.cpu),
      memory: amountFromWire(readings.memory),
      disk: disk === null ? null : { ...disk, secondsToFull: asNumberOrNull(asRecord(readings.disk).seconds_to_full) },
    },
    alerts: alertsFromWire(record.alerts),
    recentStops: stopsFromWire(record.recent_stops),
  };
}

export function settingsToWire(settings: ResourceGaugeSettings): Record<string, unknown> {
  return {
    visibility: settings.visibility,
    disk_warning_free_gb: settings.diskWarningFreeGb,
    memory_warning_percent: settings.memoryWarningPercent,
    cpu_warning_percent: settings.cpuWarningPercent,
  };
}

export async function fetchResourceGauge(): Promise<ResourceGaugeView> {
  return viewFromWire(await requestJson<unknown>("GET", apiUrl("/api/resource-gauge")));
}

export async function saveResourceGaugeSettings(settings: ResourceGaugeSettings): Promise<ResourceGaugeView> {
  return viewFromWire(await postJson<unknown>(apiUrl("/api/resource-gauge/settings"), settingsToWire(settings)));
}

/** The share of a resource in use that its bar shows, in percent; null when there is no reading. */
export function shownPercentOf(readings: ResourceReadings, resource: GaugeResource): number | null {
  switch (resource) {
    case "cpu":
      return readings.cpu === null ? null : readings.cpu.percent;
    case "memory":
      return readings.memory === null ? null : (100 * readings.memory.usedBytes) / readings.memory.totalBytes;
    case "disk":
      return readings.disk === null ? null : (100 * readings.disk.usedBytes) / readings.disk.totalBytes;
  }
}

export function alertOf(view: ResourceGaugeView, resource: GaugeResource): ResourceAlert | null {
  return view.alerts.find((alert) => alert.resource === resource) ?? null;
}

export function levelOf(view: ResourceGaugeView, resource: GaugeResource, isStale: boolean): GaugeLevel {
  if (isStale || shownPercentOf(view.readings, resource) === null) return "unknown";
  return alertOf(view, resource)?.severity ?? "normal";
}

/** Whether the tray draws anything: the bars when always shown, a warning whenever one is raised. */
export function isGaugeShown(view: ResourceGaugeView): boolean {
  return view.settings.visibility === "always" || view.alerts.length > 0;
}

export const RESOURCE_LABELS: Readonly<Record<GaugeResource, string>> = {
  cpu: "CPU",
  memory: "Memory",
  disk: "Disk",
};

/** The short label drawn over a bar. */
export const RESOURCE_SHORT_LABELS: Readonly<Record<GaugeResource, string>> = {
  cpu: "CPU",
  memory: "RAM",
  disk: "Disk",
};

const GIGABYTE = 1024 ** 3;

export function formatGigabytes(bytes: number): string {
  const gigabytes = bytes / GIGABYTE;
  return `${gigabytes >= 10 ? gigabytes.toFixed(0) : gigabytes.toFixed(1)} GB`;
}

/** A span of time in the roughest unit that says it. */
export function formatDuration(seconds: number): string {
  const minutes = seconds / 60;
  if (minutes < 1) return "under a minute";
  if (minutes < 90) return Math.round(minutes) === 1 ? "1 minute" : `${Math.round(minutes)} minutes`;
  return `${Math.round(minutes / 60)} hours`;
}

/** How long ago a moment was, roughly: "just now", "12 min ago", "3 hr ago". */
export function formatAgo(isoMoment: string, nowMs: number): string {
  const then = Date.parse(isoMoment);
  if (Number.isNaN(then)) return "";
  const minutes = Math.max(0, (nowMs - then) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${Math.round(minutes)} min ago`;
  return `${Math.round(minutes / 60)} hr ago`;
}

/** What a resource has left, in words: the card's figure for it. */
export function describeRoomLeft(readings: ResourceReadings, resource: GaugeResource): string {
  switch (resource) {
    case "cpu":
      return readings.cpu === null ? "No reading" : `${Math.max(0, 100 - readings.cpu.percent).toFixed(0)}% free`;
    case "memory":
      return readings.memory === null ? "No reading" : `${formatGigabytes(readings.memory.freeBytes)} left`;
    case "disk":
      return readings.disk === null ? "No reading" : `${formatGigabytes(readings.disk.freeBytes)} left`;
  }
}

/** The line under a resource's figure in the card: what it is out of, and where it is heading. */
export function describeCapacity(readings: ResourceReadings, resource: GaugeResource): string {
  switch (resource) {
    case "cpu": {
      const cpu = readings.cpu;
      if (cpu === null) return "";
      return `${cpu.percent.toFixed(0)}% of ${cpu.cores === 1 ? "1 core" : `${cpu.cores} cores`} in use`;
    }
    case "memory":
      return readings.memory === null ? "" : `of ${formatGigabytes(readings.memory.totalBytes)}`;
    case "disk": {
      const disk = readings.disk;
      if (disk === null) return "";
      const total = `of ${formatGigabytes(disk.totalBytes)}`;
      return disk.secondsToFull === null ? total : `${total}, full in about ${formatDuration(disk.secondsToFull)}`;
    }
  }
}

/** A bar's accessible name: the resource and what it has left. */
export function describeBar(view: ResourceGaugeView, resource: GaugeResource, isStale: boolean): string {
  const label = RESOURCE_LABELS[resource];
  if (isStale) return `${label}: not updating`;
  const alert = alertOf(view, resource);
  if (alert !== null) return `${label}: ${alert.headline}, ${alert.detail}`;
  return `${label}: ${describeRoomLeft(view.readings, resource)}`;
}

let current: ResourceGaugeView | null = null;
let receivedAtMs = 0;
let staleTimer: ReturnType<typeof setTimeout> | null = null;
let cardTimer: ReturnType<typeof setInterval> | null = null;

/** The gauge as last pushed or fetched; null before the first. */
export function currentResourceGauge(): ResourceGaugeView | null {
  return current;
}

/** Take the gauge from the shell. `redraw` runs again when the gauge has gone silent for long enough to read as
 *  no longer updating, which nothing else would repaint for. */
export function applyResourceGauge(view: ResourceGaugeView, redraw: () => void, nowMs: number = Date.now()): void {
  current = view;
  receivedAtMs = nowMs;
  if (staleTimer !== null) clearTimeout(staleTimer);
  staleTimer = setTimeout(() => {
    staleTimer = null;
    redraw();
  }, STALE_AFTER_MS + 1000);
}

/** Whether the shell has stopped sending the gauge: its figures are then not to be trusted. */
export function isResourceGaugeStale(nowMs: number = Date.now()): boolean {
  return current !== null && nowMs - receivedAtMs > STALE_AFTER_MS;
}

async function refresh(redraw: () => void): Promise<void> {
  let view: ResourceGaugeView;
  try {
    view = await fetchResourceGauge();
  } catch {
    // A missed fetch keeps the last view; the next one, or the shell's own push, replaces it.
    return;
  }
  applyResourceGauge(view, redraw);
  redraw();
}

/** Fetch the gauge now and every few seconds from here, for the open card's exact figures. */
export function startCardRefresh(redraw: () => void): void {
  if (cardTimer !== null) return;
  void refresh(redraw);
  cardTimer = setInterval(() => void refresh(redraw), CARD_REFRESH_INTERVAL_MS);
}

export function stopCardRefresh(): void {
  if (cardTimer !== null) clearInterval(cardTimer);
  cardTimer = null;
}

/** Save new settings and take the shell's answer as the gauge; throws with the shell's reason when it refuses. */
export async function saveSettings(settings: ResourceGaugeSettings, redraw: () => void): Promise<void> {
  applyResourceGauge(await saveResourceGaugeSettings(settings), redraw);
  redraw();
}

/** Forget the gauge and every timer. Test-only. */
export function resetResourceGaugeForTesting(): void {
  current = null;
  receivedAtMs = 0;
  if (staleTimer !== null) clearTimeout(staleTimer);
  staleTimer = null;
  stopCardRefresh();
}
