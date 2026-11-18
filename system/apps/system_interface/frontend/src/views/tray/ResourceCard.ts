/**
 * The resource card, hung above the tray's resource gauge while the pointer rests on it: for the disk, memory and
 * CPU, what is left in plain figures, what it is out of, where it is heading, and any warning in words. Each row
 * opens the System Monitor on that resource, which is where the user sees what is using it and stops things. Below
 * them, the programs the memory guard stopped lately, and the gauge's settings for the workspace's owner.
 */

import m from "mithril";
import {
  alertOf,
  describeCapacity,
  describeRoomLeft,
  formatAgo,
  RESOURCE_LABELS,
  type GaugeResource,
  type ResourceGaugeView,
} from "../../model/resourceGauge";
import { TrayCardFrame } from "./TrayCard";

export interface ResourceCardAttrs {
  readonly anchor: DOMRect | null;
  readonly onEnter: () => void;
  readonly onLeave: () => void;
  readonly view: ResourceGaugeView;
  readonly isStale: boolean;
  readonly now: Date;
  /** Whether the viewer may change the gauge's settings: the workspace's owner. */
  readonly canChangeSettings: boolean;
  readonly onOpen: (resource: GaugeResource) => void;
  readonly onOpenSettings: () => void;
}

/** The card lists what runs out first, first. */
const CARD_RESOURCES: readonly GaugeResource[] = ["disk", "memory", "cpu"];

function resourceRow(attrs: ResourceCardAttrs, resource: GaugeResource): m.Vnode {
  const { view, isStale } = attrs;
  const alert = isStale ? null : alertOf(view, resource);
  const figure = isStale ? "Not updating" : describeRoomLeft(view.readings, resource);
  const capacity = isStale ? "" : describeCapacity(view.readings, resource);
  return m(
    "button",
    {
      key: resource,
      type: "button",
      "data-resource-card-row": resource,
      "data-severity": alert?.severity ?? "none",
      class:
        "flex w-full cursor-pointer flex-col rounded-md border-0 bg-transparent px-1 py-1 text-left " +
        "text-primary hover:bg-fill-hover",
      onclick: () => attrs.onOpen(resource),
    },
    [
      m("span", { class: "flex items-baseline justify-between gap-2" }, [
        m("span", { class: "text-secondary" }, RESOURCE_LABELS[resource]),
        m("span", { class: "font-semibold", "data-resource-card-figure": "" }, figure),
      ]),
      capacity === ""
        ? null
        : m("span", { class: "text-right text-(length:--font-size-helper) text-faint" }, capacity),
      alert === null
        ? null
        : m(
            "span",
            {
              "data-resource-card-alert": "",
              class:
                alert.severity === "critical"
                  ? "text-(length:--font-size-helper) font-medium text-danger"
                  : "text-(length:--font-size-helper) font-medium text-primary",
            },
            `! ${alert.headline}`,
          ),
    ],
  );
}

export const ResourceCard: m.Component<ResourceCardAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    const stops = attrs.view.recentStops;
    return m(
      TrayCardFrame,
      { anchor: attrs.anchor, label: "Workspace resources", onEnter: attrs.onEnter, onLeave: attrs.onLeave },
      [
        m("div", { class: "flex flex-col gap-0.5", "data-resource-card": "" }, [
          CARD_RESOURCES.map((resource) => resourceRow(attrs, resource)),
        ]),
        stops.length === 0
          ? null
          : m(
              "div",
              { class: "flex flex-col gap-0.5 border-t border-subtle pt-1.5", "data-resource-card-stops": "" },
              [
                m("div", { class: "px-1 text-(length:--font-size-helper) text-faint" }, "Stopped to free memory"),
                stops.map((stop) =>
                  m(
                    "button",
                    {
                      key: stop.at,
                      type: "button",
                      class:
                        "flex w-full cursor-pointer items-baseline justify-between gap-2 rounded-md border-0 " +
                        "bg-transparent px-1 py-0.5 text-left text-primary hover:bg-fill-hover",
                      onclick: () => attrs.onOpen("memory"),
                    },
                    [
                      m("span", { class: "min-w-0 flex-1 first-letter:uppercase" }, stop.what),
                      m(
                        "span",
                        { class: "shrink-0 text-(length:--font-size-helper) text-faint" },
                        formatAgo(stop.at, attrs.now.getTime()),
                      ),
                    ],
                  ),
                ),
              ],
            ),
        m("div", { class: "flex flex-col gap-0.5 border-t border-subtle pt-1.5" }, [
          m(
            "div",
            { class: "px-1 text-(length:--font-size-helper) text-faint" },
            "Click a row to see what is using it.",
          ),
          attrs.canChangeSettings
            ? m(
                "button",
                {
                  type: "button",
                  "data-resource-card-settings": "",
                  class:
                    "flex w-full cursor-pointer items-center rounded-md border-0 bg-transparent px-1 py-0.5 " +
                    "text-left text-primary hover:bg-fill-hover",
                  onclick: attrs.onOpenSettings,
                },
                "Warnings and settings...",
              )
            : null,
        ]),
      ],
    );
  },
};
