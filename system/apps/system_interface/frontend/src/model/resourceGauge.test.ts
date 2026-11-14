// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CARD_REFRESH_INTERVAL_MS,
  STALE_AFTER_MS,
  alertOf,
  applyResourceGauge,
  currentResourceGauge,
  describeBar,
  describeCapacity,
  describeRoomLeft,
  formatAgo,
  formatDuration,
  isGaugeShown,
  isResourceGaugeStale,
  levelOf,
  resetResourceGaugeForTesting,
  saveSettings,
  settingsToWire,
  startCardRefresh,
  stopCardRefresh,
  systemMonitorPathOf,
  viewFromWire,
  type ResourceGaugeView,
} from "./resourceGauge";

const GIB = 1024 ** 3;

function wire(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    settings: {
      visibility: "when_warning",
      disk_warning_free_gb: 5,
      memory_warning_percent: null,
      cpu_warning_percent: null,
    },
    readings: {
      cpu: { percent: 7, recent_percent: 5, cores: 1 },
      memory: { used_bytes: 5 * GIB, free_bytes: 3 * GIB, total_bytes: 8 * GIB },
      disk: { used_bytes: 60 * GIB, free_bytes: 40 * GIB, total_bytes: 100 * GIB, seconds_to_full: null },
    },
    alerts: [],
    recent_stops: [],
    ...overrides,
  };
}

function view(overrides: Record<string, unknown> = {}): ResourceGaugeView {
  return viewFromWire(wire(overrides));
}

const DISK_ALERT = { resource: "disk", severity: "critical", headline: "Disk is almost full", detail: "0.9 GB left" };

function stubFetch(body: unknown): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(body))));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  resetResourceGaugeForTesting();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("resource gauge model", () => {
  it("reads the shell's view and writes its settings in the shell's field names", () => {
    const parsed = view({
      alerts: [DISK_ALERT, { resource: "gpu", severity: "warning", headline: "?", detail: "" }],
      recent_stops: [{ at: "2026-11-12T10:00:00.000000Z", what: "a chat's agent" }, { at: 3 }],
    });
    expect(parsed.readings.disk).toEqual({
      usedBytes: 60 * GIB,
      freeBytes: 40 * GIB,
      totalBytes: 100 * GIB,
      secondsToFull: null,
    });
    expect(parsed.alerts).toEqual([
      { resource: "disk", severity: "critical", headline: "Disk is almost full", detail: "0.9 GB left" },
    ]);
    expect(parsed.recentStops).toEqual([{ at: "2026-11-12T10:00:00.000000Z", what: "a chat's agent" }]);
    expect(settingsToWire({ ...parsed.settings, diskWarningFreeGb: 2.5, memoryWarningPercent: 90 })).toEqual({
      visibility: "when_warning",
      disk_warning_free_gb: 2.5,
      memory_warning_percent: 90,
      cpu_warning_percent: null,
    });
  });

  it("reads a gauge it cannot make sense of as one with no readings, never throwing", () => {
    const parsed = viewFromWire({ readings: { disk: { used_bytes: "many" } }, alerts: "none" });
    expect(parsed.readings).toEqual({ cpu: null, memory: null, disk: null });
    expect(parsed.alerts).toEqual([]);
    expect(parsed.settings.visibility).toBe("when_warning");
    expect(viewFromWire(null).recentStops).toEqual([]);
  });

  it("draws a bar by its alert, and as unknown when there is no reading or the gauge went silent", () => {
    const alerting = view({ alerts: [DISK_ALERT] });
    expect(levelOf(alerting, "disk", false)).toBe("critical");
    expect(levelOf(alerting, "memory", false)).toBe("normal");
    expect(levelOf(alerting, "disk", true)).toBe("unknown");
    expect(levelOf(view({ readings: { cpu: null, memory: null, disk: null } }), "cpu", false)).toBe("unknown");
    expect(alertOf(alerting, "memory")).toBeNull();
  });

  it("shows the bars when always shown, and a warning whenever one is raised", () => {
    expect(isGaugeShown(view())).toBe(false);
    expect(isGaugeShown(view({ alerts: [DISK_ALERT] }))).toBe(true);
    expect(isGaugeShown(view({ settings: { visibility: "always" } }))).toBe(true);
  });

  it("says what is left, what it is out of, and where it is heading", () => {
    const readings = view().readings;
    expect(describeRoomLeft(readings, "disk")).toBe("40 GB left");
    expect(describeRoomLeft(readings, "memory")).toBe("3.0 GB left");
    expect(describeRoomLeft(readings, "cpu")).toBe("93% free");
    expect(describeRoomLeft({ ...readings, cpu: null }, "cpu")).toBe("No reading");
    expect(describeCapacity(readings, "disk")).toBe("of 100 GB");
    expect(describeCapacity(readings, "cpu")).toBe("7% of 1 core in use");
    const filling = { ...readings, disk: { ...readings.disk!, secondsToFull: 12 * 60 } };
    expect(describeCapacity(filling, "disk")).toBe("of 100 GB, full in about 12 minutes");
    expect(describeBar(view({ alerts: [DISK_ALERT] }), "disk", false)).toBe("Disk: Disk is almost full, 0.9 GB left");
    expect(describeBar(view(), "memory", false)).toBe("Memory: 3.0 GB left");
    expect(describeBar(view(), "memory", true)).toBe("Memory: not updating");
    expect(systemMonitorPathOf("memory")).toBe("/memory");
  });

  it("says spans and moments roughly", () => {
    expect(formatDuration(30)).toBe("under a minute");
    expect(formatDuration(60)).toBe("1 minute");
    expect(formatDuration(3 * 3600)).toBe("3 hours");
    const now = Date.parse("2026-11-12T12:00:00Z");
    expect(formatAgo("2026-11-12T11:59:40.000000Z", now)).toBe("just now");
    expect(formatAgo("2026-11-12T11:48:00.000000Z", now)).toBe("12 min ago");
    expect(formatAgo("2026-11-12T09:00:00.000000Z", now)).toBe("3 hr ago");
    expect(formatAgo("not a moment", now)).toBe("");
  });

  it("reads as stale once the shell has been silent past its heartbeat, and redraws when that happens", () => {
    vi.useFakeTimers();
    const redraw = vi.fn();
    expect(isResourceGaugeStale()).toBe(false);
    applyResourceGauge(view(), redraw);
    expect(currentResourceGauge()).not.toBeNull();
    vi.advanceTimersByTime(STALE_AFTER_MS - 1000);
    expect(isResourceGaugeStale()).toBe(false);
    expect(redraw).not.toHaveBeenCalled();
    vi.advanceTimersByTime(2500);
    expect(isResourceGaugeStale()).toBe(true);
    expect(redraw).toHaveBeenCalledTimes(1);
    // The next push is fresh again.
    applyResourceGauge(view(), redraw);
    expect(isResourceGaugeStale()).toBe(false);
  });

  it("fetches while the card is open, and stops when it closes", async () => {
    vi.useFakeTimers();
    const fetchMock = stubFetch(wire({ alerts: [DISK_ALERT] }));
    const redraw = vi.fn();
    startCardRefresh(redraw);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(currentResourceGauge()?.alerts[0].headline).toBe("Disk is almost full");
    expect(redraw).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(CARD_REFRESH_INTERVAL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    stopCardRefresh();
    await vi.advanceTimersByTimeAsync(CARD_REFRESH_INTERVAL_MS * 2);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps the last gauge when a fetch fails", async () => {
    vi.useFakeTimers();
    const redraw = vi.fn();
    applyResourceGauge(view(), redraw);
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    startCardRefresh(redraw);
    await vi.advanceTimersByTimeAsync(0);
    expect(currentResourceGauge()?.readings.disk?.freeBytes).toBe(40 * GIB);
  });

  it("takes the shell's answer to a save as the gauge", async () => {
    const fetchMock = stubFetch(wire({ settings: { visibility: "always", disk_warning_free_gb: 2 } }));
    const redraw = vi.fn();
    await saveSettings({ ...view().settings, visibility: "always", diskWarningFreeGb: 2 }, redraw);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({
      visibility: "always",
      disk_warning_free_gb: 2,
      memory_warning_percent: null,
      cpu_warning_percent: null,
    });
    expect(currentResourceGauge()?.settings.visibility).toBe("always");
    expect(redraw).toHaveBeenCalled();
  });
});
