// @vitest-environment jsdom
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { viewFromWire, type ResourceGaugeView } from "../../model/resourceGauge";
import { ResourceCard, type ResourceCardAttrs } from "./ResourceCard";
import { ResourceGauge, type ResourceGaugeAttrs } from "./ResourceGauge";

const GIB = 1024 ** 3;
const DISK_ALERT = { resource: "disk", severity: "warning", headline: "Disk space is low", detail: "3.2 GB left" };
const MEMORY_ALERT = { resource: "memory", severity: "warning", headline: "Memory is nearly full", detail: "0.4 GB" };

function gaugeView(overrides: Record<string, unknown> = {}): ResourceGaugeView {
  return viewFromWire({
    settings: { visibility: "always", disk_warning_free_gb: 5 },
    readings: {
      cpu: { percent: 12, recent_percent: 10, cores: 4 },
      memory: { used_bytes: 2 * GIB, free_bytes: 6 * GIB, total_bytes: 8 * GIB },
      disk: { used_bytes: 40 * GIB, free_bytes: 60 * GIB, total_bytes: 100 * GIB, seconds_to_full: null },
    },
    alerts: [],
    recent_stops: [],
    ...overrides,
  });
}

function render(overrides: Partial<ResourceGaugeAttrs> = {}): HTMLElement {
  const attrs: ResourceGaugeAttrs = {
    view: gaugeView(),
    isStale: false,
    onOpen: vi.fn(),
    onContextMenu: vi.fn(),
    onHoverStart: vi.fn(),
    onHoverEnd: vi.fn(),
    ...overrides,
  };
  return mountView(() => m("div", m(ResourceGauge, attrs)));
}

function renderCard(overrides: Partial<ResourceCardAttrs> = {}): HTMLElement {
  const attrs: ResourceCardAttrs = {
    anchor: null,
    onEnter: vi.fn(),
    onLeave: vi.fn(),
    view: gaugeView(),
    isStale: false,
    now: new Date("2026-11-12T12:00:00Z"),
    canChangeSettings: true,
    onOpen: vi.fn(),
    onOpenSettings: vi.fn(),
    ...overrides,
  };
  return mountView(() => m("div", m(ResourceCard, attrs)));
}

function barOf(root: HTMLElement, resource: string): HTMLButtonElement {
  return root.querySelector(`[data-resource-gauge-bar="${resource}"]`) as HTMLButtonElement;
}

afterEach(unmountViews);

describe("ResourceGauge", () => {
  it("draws a bar per resource when always shown, and opens the resource's view on a click", () => {
    const onOpen = vi.fn();
    const root = render({ onOpen });
    expect(barOf(root, "cpu").textContent).toBe("CPU");
    expect(barOf(root, "memory").textContent).toBe("RAM");
    expect(barOf(root, "disk").getAttribute("aria-label")).toBe("Disk: 60 GB left");
    const diskFill = barOf(root, "disk").querySelector("[data-resource-gauge-fill]") as HTMLElement;
    expect(diskFill.style.width).toBe("40%");
    expect(root.querySelector("[data-resource-gauge-alert]")).toBeNull();
    barOf(root, "disk").click();
    expect(onOpen).toHaveBeenCalledWith("disk");
  });

  it("draws nothing before the gauge arrives, or when shown only during a warning and none is raised", () => {
    expect(render({ view: null }).querySelector("[data-resource-gauge]")).toBeNull();
    const quiet = gaugeView({ settings: { visibility: "when_warning" } });
    expect(render({ view: quiet }).querySelector("[data-resource-gauge]")).toBeNull();
  });

  it("says a warning in words, with no bars unless they are always shown", () => {
    const onOpen = vi.fn();
    const view = gaugeView({ settings: { visibility: "when_warning" }, alerts: [DISK_ALERT, MEMORY_ALERT] });
    const root = render({ view, onOpen });
    const alert = root.querySelector("[data-resource-gauge-alert]") as HTMLButtonElement;
    expect(alert.textContent).toBe("!Disk space is low+1");
    expect(alert.getAttribute("aria-label")).toBe("Disk space is low: 3.2 GB left");
    expect(alert.dataset.severity).toBe("warning");
    expect(root.querySelector("[data-resource-gauge-bar]")).toBeNull();
    alert.click();
    expect(onOpen).toHaveBeenCalledWith("disk");
  });

  it("marks an alerting bar in its label as well as its colour", () => {
    const root = render({ view: gaugeView({ alerts: [DISK_ALERT] }) });
    expect(barOf(root, "disk").textContent).toBe("Disk !");
    expect(barOf(root, "disk").dataset.level).toBe("warning");
    expect(barOf(root, "memory").dataset.level).toBe("normal");
  });

  it("draws a gauge that stopped updating as empty bars and no warning", () => {
    const root = render({ view: gaugeView({ alerts: [DISK_ALERT] }), isStale: true });
    expect(root.querySelector("[data-resource-gauge-alert]")).toBeNull();
    expect(barOf(root, "disk").dataset.level).toBe("unknown");
    expect(barOf(root, "disk").getAttribute("aria-label")).toBe("Disk: not updating");
    const fill = barOf(root, "disk").querySelector("[data-resource-gauge-fill]") as HTMLElement;
    expect(fill.style.width).toBe("0%");
    const hidden = gaugeView({ settings: { visibility: "when_warning" }, alerts: [DISK_ALERT] });
    expect(render({ view: hidden, isStale: true }).querySelector("[data-resource-gauge]")).toBeNull();
  });

  it("opens its card on hover and its menu on a right-click", () => {
    const onContextMenu = vi.fn();
    const onHoverStart = vi.fn();
    const onHoverEnd = vi.fn();
    const root = render({ onContextMenu, onHoverStart, onHoverEnd });
    const gauge = root.querySelector("[data-resource-gauge]") as HTMLElement;
    gauge.dispatchEvent(new MouseEvent("mouseenter"));
    expect(onHoverStart).toHaveBeenCalledWith(gauge);
    gauge.dispatchEvent(new MouseEvent("mouseleave"));
    expect(onHoverEnd).toHaveBeenCalledTimes(1);
    gauge.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 40, clientY: 50 }));
    expect(onContextMenu).toHaveBeenCalledWith(40, 50);
  });
});

describe("ResourceCard", () => {
  function rowOf(root: HTMLElement, resource: string): HTMLButtonElement {
    return root.querySelector(`[data-resource-card-row="${resource}"]`) as HTMLButtonElement;
  }

  it("lists what each resource has left, disk first, and opens its view on a click", () => {
    const onOpen = vi.fn();
    const root = renderCard({ onOpen });
    const rows = [...root.querySelectorAll("[data-resource-card-row]")].map((row) => row.textContent);
    expect(rows).toEqual([
      "Disk60 GB leftof 100 GB",
      "Memory6.0 GB leftof 8.0 GB",
      "CPU88% free12% of 4 cores in use",
    ]);
    rowOf(root, "memory").click();
    expect(onOpen).toHaveBeenCalledWith("memory");
  });

  it("says a warning and the time to full in the resource's row", () => {
    const view = gaugeView({
      readings: {
        cpu: null,
        memory: null,
        disk: { used_bytes: 97 * GIB, free_bytes: 3 * GIB, total_bytes: 100 * GIB, seconds_to_full: 600 },
      },
      alerts: [DISK_ALERT],
    });
    const root = renderCard({ view });
    expect(rowOf(root, "disk").textContent).toBe(
      "Disk3.0 GB leftof 100 GB, full in about 10 minutes! Disk space is low",
    );
    expect(rowOf(root, "disk").dataset.severity).toBe("warning");
    expect(rowOf(root, "memory").textContent).toBe("MemoryNo reading");
  });

  it("lists the programs stopped to free memory, and says nothing of them when there are none", () => {
    expect(renderCard().querySelector("[data-resource-card-stops]")).toBeNull();
    unmountViews();
    const onOpen = vi.fn();
    const view = gaugeView({ recent_stops: [{ at: "2026-11-12T11:48:00.000000Z", what: "a chat's agent" }] });
    const root = renderCard({ view, onOpen });
    const stops = root.querySelector("[data-resource-card-stops]") as HTMLElement;
    expect(stops.textContent).toBe("Stopped to free memorya chat's agent12 min ago");
    (stops.querySelector("button") as HTMLButtonElement).click();
    expect(onOpen).toHaveBeenCalledWith("memory");
  });

  it("offers the settings to the owner alone, and says so when the gauge stopped updating", () => {
    const onOpenSettings = vi.fn();
    const root = renderCard({ onOpenSettings });
    (root.querySelector("[data-resource-card-settings]") as HTMLButtonElement).click();
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
    unmountViews();
    expect(renderCard({ canChangeSettings: false }).querySelector("[data-resource-card-settings]")).toBeNull();
    unmountViews();
    const stale = renderCard({ isStale: true, view: gaugeView({ alerts: [DISK_ALERT] }) });
    expect(rowOf(stale, "disk").textContent).toBe("DiskNot updating");
  });
});
