/**
 * The small pieces the app tiles and the detail page share: an app's icon, its room badge, the meter of what it
 * needs against what is free, and its install progress.
 */

import m from "mithril";
import { badgeClass } from "@imbue/workspace-ui/src/components/Badge";
import type { BadgeTone } from "@imbue/workspace-ui/src/components/Badge";
import type { InstallStatus, RoomVerdict } from "../models/Store";
import { VERDICT_LABELS } from "../room";

const VERDICT_TONES: Record<RoomVerdict, BadgeTone> = { FITS: "success", TIGHT: "warning", WONT_FIT: "danger" };

/** The steps an install shows, in order. */
export const INSTALL_STAGES: readonly { status: InstallStatus; label: string }[] = [
  { status: "QUEUED", label: "Waiting" },
  { status: "DOWNLOADING", label: "Downloading" },
  { status: "CONFIGURING", label: "Setting up" },
  { status: "STARTING", label: "Starting" },
];

export function verdictBadge(verdict: RoomVerdict): m.Vnode {
  return m("span", { class: badgeClass(VERDICT_TONES[verdict]), "data-verdict": verdict }, VERDICT_LABELS[verdict]);
}

/** An app's icon at ``size`` px, drawn from a URL (an <img>, so an icon's markup never runs in the page). */
export function appIcon(src: string, size: number): m.Vnode {
  return src === ""
    ? m("span", {
        class: "block shrink-0 bg-surface-secondary",
        style: `width:${size}px;height:${size}px;border-radius:${size * 0.32}px`,
        "aria-hidden": "true",
      })
    : m("img", { src, alt: "", width: size, height: size, class: "block shrink-0", draggable: false });
}

/** A thin bar: how much of what is free the app would take. */
export function roomMeter(neededMb: number, freeMb: number): m.Vnode {
  const isOver = neededMb > freeMb;
  const percent = isOver || freeMb <= 0 ? 100 : Math.max(2, (neededMb / freeMb) * 100);
  return m(
    "div",
    { class: "mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-surface-secondary", "data-meter": "" },
    m("div", { class: `h-full rounded-full ${isOver ? "bg-danger" : "bg-accent"}`, style: `width:${percent}%` }),
  );
}

/** Where an install stands, as a label and four segments; an uninstall is one line. */
export function installProgress(status: InstallStatus, logHref: string | null): m.Vnode {
  const logLink =
    logHref === null
      ? null
      : m("a", { class: "text-secondary hover:text-primary", href: logHref, target: "_blank" }, "Install log");
  if (status === "UNINSTALLING") {
    return m("div", { class: "flex items-center justify-between type-helper", "data-install-stage": status }, [
      m("span", { class: "flex items-center gap-2 text-primary" }, [
        m("span", { class: "spinner spinner--sm" }),
        "Uninstalling…",
      ]),
      logLink,
    ]);
  }
  const current = Math.max(
    0,
    INSTALL_STAGES.findIndex((entry) => entry.status === status),
  );
  return m("div", { class: "flex flex-col gap-1.5", "data-install-stage": status }, [
    m("div", { class: "flex items-center justify-between type-helper" }, [
      m("span", { class: "flex items-center gap-2 text-primary" }, [
        m("span", { class: "spinner spinner--sm" }),
        `${INSTALL_STAGES[current].label}…`,
      ]),
      logLink,
    ]),
    m(
      "div",
      { class: "flex gap-1" },
      INSTALL_STAGES.map((entry, index) =>
        m("span", {
          class: `h-1 flex-1 rounded-full ${
            index < current ? "bg-accent" : index === current ? "bg-accent/50" : "bg-surface-secondary"
          }`,
          title: entry.label,
        }),
      ),
    ),
  ]);
}
