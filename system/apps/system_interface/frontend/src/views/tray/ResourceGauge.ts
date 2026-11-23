/**
 * The tray's resource gauge. A raised disk or memory warning is a pill that says what is wrong in words ("Disk
 * space is low"); a raised CPU warning is the CPU bar, drawn in the warning colour. With the bars always shown,
 * there are three small bars for the workspace's CPU, memory and disk.
 * Resting the pointer on either opens the resource card; a click opens the System Monitor on that resource's view;
 * a right-click opens the gauge's menu.
 *
 * Nothing here says a state in colour alone: a warning has its words and a mark, and a bar that is not being
 * updated is drawn dashed and empty.
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility name a contiguous
 * literal.
 */

import m from "mithril";
import {
  RESOURCE_SHORT_LABELS,
  describeBar,
  isGaugeShown,
  levelOf,
  pillAlertsOf,
  shownBarsOf,
  shownPercentOf,
  type GaugeLevel,
  type GaugeResource,
  type ResourceAlert,
  type ResourceGaugeView,
} from "../../model/resourceGauge";

export interface ResourceGaugeAttrs {
  readonly view: ResourceGaugeView | null;
  /** The shell has stopped sending the gauge: its figures are not drawn. */
  readonly isStale: boolean;
  readonly onOpen: (resource: GaugeResource) => void;
  /** A right-click on the gauge, whose menu the shell's element menu opens. */
  readonly onContextMenu: () => void;
  readonly onHoverStart: (element: HTMLElement) => void;
  readonly onHoverEnd: () => void;
}

const BAR_FILL_CLASS: Readonly<Record<GaugeLevel, string>> = {
  normal: "bg-accent",
  warning: "bg-warning",
  critical: "bg-danger",
  unknown: "bg-transparent",
};

const BAR_LABEL_CLASS: Readonly<Record<GaugeLevel, string>> = {
  normal: "text-secondary",
  warning: "font-semibold text-primary",
  critical: "font-semibold text-danger",
  unknown: "text-faint",
};

function bar(
  view: ResourceGaugeView,
  resource: GaugeResource,
  isStale: boolean,
  onOpen: (resource: GaugeResource) => void,
): m.Vnode {
  const level = levelOf(view, resource, isStale);
  const shown = level === "unknown" ? null : shownPercentOf(view.readings, resource);
  const width = shown === null ? 0 : Math.min(Math.max(shown, 0), 100);
  const isAlerting = level === "warning" || level === "critical";
  return m(
    "button",
    {
      key: resource,
      type: "button",
      "data-resource-gauge-bar": resource,
      "data-level": level,
      class:
        "flex w-9 shrink-0 cursor-pointer flex-col items-stretch gap-1 rounded-md border-0 bg-transparent px-1 py-1 " +
        "outline-none hover:bg-fill-hover focus-visible:ring-2 focus-visible:ring-accent",
      "aria-label": describeBar(view, resource, isStale),
      onclick: () => onOpen(resource),
    },
    [
      m(
        "span",
        { class: `text-center text-(length:--font-size-tag) leading-none ${BAR_LABEL_CLASS[level]}` },
        isAlerting ? `${RESOURCE_SHORT_LABELS[resource]} !` : RESOURCE_SHORT_LABELS[resource],
      ),
      m(
        "span",
        {
          class:
            level === "unknown"
              ? "relative block h-1 overflow-hidden rounded-full border border-dashed border-strong"
              : "relative block h-1 overflow-hidden rounded-full bg-fill-active",
        },
        [
          m("span", {
            "data-resource-gauge-fill": "",
            class: `absolute inset-y-0 left-0 block rounded-full ${BAR_FILL_CLASS[level]}`,
            style: { width: `${width}%` },
          }),
        ],
      ),
    ],
  );
}

function pill(alert: ResourceAlert, extraCount: number, onOpen: (resource: GaugeResource) => void): m.Vnode {
  const isCritical = alert.severity === "critical";
  return m(
    "button",
    {
      type: "button",
      "data-resource-gauge-alert": alert.resource,
      "data-severity": alert.severity,
      class:
        "flex h-7 shrink-0 cursor-pointer items-center gap-1.5 rounded-full border px-2.5 outline-none " +
        "text-(length:--font-size-row) font-medium focus-visible:ring-2 focus-visible:ring-accent " +
        (isCritical
          ? "border-danger-border bg-danger-surface text-danger"
          : "border-warning bg-warning-surface text-primary"),
      "aria-label": `${alert.headline}: ${alert.detail}`,
      onclick: () => onOpen(alert.resource),
    },
    [
      m("span", { "aria-hidden": "true", class: "font-bold" }, "!"),
      m("span", { class: "whitespace-nowrap" }, alert.headline),
      extraCount > 0 ? m("span", { class: "text-secondary", "data-resource-gauge-more": "" }, `+${extraCount}`) : null,
    ],
  );
}

export const ResourceGauge: m.Component<ResourceGaugeAttrs> = {
  view(vnode) {
    const { view, isStale, onOpen, onContextMenu, onHoverStart, onHoverEnd } = vnode.attrs;
    if (view === null || !isGaugeShown(view)) return null;
    const pillAlerts = isStale ? [] : pillAlertsOf(view);
    const alert = pillAlerts[0] ?? null;
    const bars = shownBarsOf(view, isStale);
    if (alert === null && bars.length === 0) return null;
    return m(
      "div",
      {
        "data-resource-gauge": "",
        role: "group",
        "aria-label": "Workspace resources",
        class: "resource-gauge flex shrink-0 items-center gap-1",
        oncontextmenu: onContextMenu,
        onmouseenter: (event: MouseEvent) => onHoverStart(event.currentTarget as HTMLElement),
        onmouseleave: onHoverEnd,
      },
      [
        alert === null ? null : pill(alert, pillAlerts.length - 1, onOpen),
        bars.map((resource) => bar(view, resource, isStale, onOpen)),
      ],
    );
  },
};
