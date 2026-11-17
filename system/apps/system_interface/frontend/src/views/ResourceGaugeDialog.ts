/**
 * The resource gauge's settings: whether the tray always shows the gauge's bars or stays empty until a warning,
 * and the level each resource warns at (or no warning). The disk warns on the space left, in GB; memory and CPU on
 * the share in use. Opened from "Resource gauge..." on the backdrop's context menu, the gauge's own menu, and the
 * resource card, for the workspace's owner. Saving keeps the settings in the shell, which judges the warnings and
 * sends the notifications.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { MODAL_LABEL_CLASS, MODAL_MESSAGE_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";
import { SegmentedControl } from "@imbue/workspace-ui/src/components/segmentedControl";
import {
  MAX_DISK_WARNING_FREE_GB,
  MAX_WARNING_PERCENT,
  MIN_DISK_WARNING_FREE_GB,
  MIN_WARNING_PERCENT,
  RESOURCE_LABELS,
  type GaugeResource,
  type GaugeVisibility,
  type ResourceGaugeSettings,
} from "../model/resourceGauge";

export interface ResourceGaugeDialogAttrs {
  readonly settings: ResourceGaugeSettings;
  readonly onSave: (settings: ResourceGaugeSettings) => Promise<void>;
  readonly onClose: () => void;
}

export const RESOURCE_GAUGE_DIALOG_WIDTH = 460;

const VISIBILITY_SEGMENTS = [
  { value: "always" as const, label: "Always", attrs: { "data-gauge-visibility": "always" } },
  {
    value: "when_warning" as const,
    label: "Only during a warning",
    attrs: { "data-gauge-visibility": "when_warning" },
  },
];

/** The rows in the order they are listed: what runs out first, first. */
const DIALOG_RESOURCES: readonly GaugeResource[] = ["disk", "memory", "cpu"];

const WARNING_SENTENCES: Readonly<Record<GaugeResource, string>> = {
  disk: "Warn when disk space left falls to",
  memory: "Warn when memory in use reaches",
  cpu: "Warn when CPU use (over two minutes) reaches",
};

const UNITS: Readonly<Record<GaugeResource, string>> = { disk: "GB", memory: "%", cpu: "%" };

/** The level a resource starts at when its warning is turned on. */
const TURNED_ON_LEVELS: Readonly<Record<GaugeResource, number>> = { disk: 5, memory: 90, cpu: 90 };

function levelOf(settings: ResourceGaugeSettings, resource: GaugeResource): number | null {
  switch (resource) {
    case "disk":
      return settings.diskWarningFreeGb;
    case "memory":
      return settings.memoryWarningPercent;
    case "cpu":
      return settings.cpuWarningPercent;
  }
}

function withLevel(
  settings: ResourceGaugeSettings,
  resource: GaugeResource,
  level: number | null,
): ResourceGaugeSettings {
  switch (resource) {
    case "disk":
      return { ...settings, diskWarningFreeGb: level };
    case "memory":
      return { ...settings, memoryWarningPercent: level };
    case "cpu":
      return { ...settings, cpuWarningPercent: level };
  }
}

/** The typed level as a number its resource takes, or null when it is not one: GB for the disk (a decimal is
 *  fine), a whole percentage for memory and CPU. */
export function parseLevelText(resource: GaugeResource, text: string): number | null {
  const trimmed = text.trim();
  if (resource === "disk") {
    if (!/^\d+(\.\d+)?$/.test(trimmed)) return null;
    const value = Number(trimmed);
    return value >= MIN_DISK_WARNING_FREE_GB && value <= MAX_DISK_WARNING_FREE_GB ? value : null;
  }
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value >= MIN_WARNING_PERCENT && value <= MAX_WARNING_PERCENT ? value : null;
}

const RANGE_ERROR =
  `Disk space is ${MIN_DISK_WARNING_FREE_GB} to ${MAX_DISK_WARNING_FREE_GB} GB; ` +
  `memory and CPU are whole numbers from ${MIN_WARNING_PERCENT} to ${MAX_WARNING_PERCENT}.`;

export function ResourceGaugeDialog(): m.Component<ResourceGaugeDialogAttrs> {
  let visibility: GaugeVisibility = "when_warning";
  let enabled: Record<GaugeResource, boolean> = { cpu: false, memory: false, disk: false };
  /** The levels as typed, so a half-typed number is not thrown away. */
  let texts: Record<GaugeResource, string> = { cpu: "", memory: "", disk: "" };
  let error: string | null = null;
  let isSaving = false;

  function draft(base: ResourceGaugeSettings): ResourceGaugeSettings | null {
    let result: ResourceGaugeSettings = { ...base, visibility };
    for (const resource of DIALOG_RESOURCES) {
      if (!enabled[resource]) {
        result = withLevel(result, resource, null);
        continue;
      }
      const level = parseLevelText(resource, texts[resource]);
      if (level === null) return null;
      result = withLevel(result, resource, level);
    }
    return result;
  }

  async function save(attrs: ResourceGaugeDialogAttrs): Promise<void> {
    const next = draft(attrs.settings);
    if (next === null) {
      error = RANGE_ERROR;
      return;
    }
    error = null;
    isSaving = true;
    try {
      await attrs.onSave(next);
      attrs.onClose();
    } catch (failure) {
      error = `Could not save: ${(failure as Error).message}`;
    } finally {
      isSaving = false;
      m.redraw();
    }
  }

  function warningRow(resource: GaugeResource): m.Vnode {
    return m("div", { key: resource, class: "flex items-center gap-2 py-1", "data-gauge-warning": resource }, [
      m("label", { class: "flex flex-1 cursor-pointer items-center gap-2 text-primary" }, [
        m("input", {
          type: "checkbox",
          "data-gauge-warning-enabled": resource,
          checked: enabled[resource],
          onchange: (event: Event) => {
            enabled = { ...enabled, [resource]: (event.target as HTMLInputElement).checked };
          },
        }),
        m("span", WARNING_SENTENCES[resource]),
      ]),
      m("div", { class: "w-16 shrink-0" }, [
        m("input", {
          type: "text",
          inputmode: resource === "disk" ? "decimal" : "numeric",
          "aria-label": `${RESOURCE_LABELS[resource]} warning level, in ${UNITS[resource] === "GB" ? "GB" : "percent"}`,
          "data-gauge-warning-level": resource,
          class: inputClass({ extra: "text-right" }),
          disabled: !enabled[resource],
          value: texts[resource],
          oninput: (event: Event) => {
            texts = { ...texts, [resource]: (event.target as HTMLInputElement).value };
          },
        }),
      ]),
      m("span", { class: "w-6 text-secondary" }, UNITS[resource]),
    ]);
  }

  return {
    oninit(vnode) {
      const settings = vnode.attrs.settings;
      visibility = settings.visibility;
      for (const resource of DIALOG_RESOURCES) {
        const level = levelOf(settings, resource);
        enabled = { ...enabled, [resource]: level !== null };
        texts = { ...texts, [resource]: String(level ?? TURNED_ON_LEVELS[resource]) };
      }
    },
    view(vnode) {
      const attrs = vnode.attrs;
      return m(
        Modal,
        {
          onDismiss: attrs.onClose,
          onEscape: attrs.onClose,
          title: "Resource gauge",
          width: RESOURCE_GAUGE_DIALOG_WIDTH,
          card: { "data-resource-gauge-dialog": true },
          actions: [
            m(Button, { extra: "resource-gauge-cancel", onclick: attrs.onClose }, "Cancel"),
            m(
              Button,
              {
                variant: "primary",
                extra: "resource-gauge-save",
                disabled: isSaving,
                onclick: () => void save(attrs),
              },
              "Save",
            ),
          ],
        },
        [
          m(
            "p",
            { class: MODAL_MESSAGE_CLASS },
            "The gauge in the taskbar shows how much disk, memory and CPU the workspace has left. A warning always " +
              "shows there, in words.",
          ),
          m("span", { class: MODAL_LABEL_CLASS }, "Show the bars"),
          m("div", { class: "mb-4 flex" }, [
            m(SegmentedControl<GaugeVisibility>, {
              label: "When to show the gauge's bars",
              segments: VISIBILITY_SEGMENTS,
              value: visibility,
              onSelect: (value: GaugeVisibility) => {
                visibility = value;
              },
            }),
          ]),
          m("span", { class: MODAL_LABEL_CLASS }, "Warnings"),
          m(
            "div",
            DIALOG_RESOURCES.map((resource) => warningRow(resource)),
          ),
          m(
            "p",
            { class: "mt-2 mb-0 text-secondary type-body" },
            "A warning sends one notification to Imbue Studio and tells the agents working here. The disk also " +
              "warns when it is on course to fill within 15 minutes.",
          ),
          error === null ? null : m("p", { class: "mt-2 mb-0 text-danger type-body", "data-gauge-error": "" }, error),
        ],
      );
    },
  };
}
