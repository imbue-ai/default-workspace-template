import m from "mithril";

import { badgeClass, type BadgeTone } from "@imbue/workspace-ui/src/components/Badge";

import type { Health, RoutineRow } from "../models/api";

export function toneOf(health: Health): BadgeTone {
  switch (health) {
    case "WORKING":
    case "DONE":
      return "success";
    case "FAILING":
    case "BROKEN":
      return "danger";
    case "OVERDUE":
    case "NEEDS_USER":
      return "warning";
    case "OFF":
    case "NOT_RUN_YET":
      return "neutral";
  }
}

/** The one word about a routine, as a chip; a run in flight shows as such, whatever the health. */
export function healthPill(row: RoutineRow): m.Vnode {
  if (row.is_running && row.health !== "NEEDS_USER") {
    return m("span", { class: badgeClass("accent", { extra: "health-pill" }), "data-health": "RUNNING" }, [
      m("span", { class: "inline-block size-1.5 animate-pulse rounded-full bg-current" }),
      "Running",
    ]);
  }
  return m(
    "span",
    { class: badgeClass(toneOf(row.health), { extra: "health-pill" }), "data-health": row.health },
    row.health_text,
  );
}
