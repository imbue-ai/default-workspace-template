/** How the page words sizes and the store's room verdicts (the verdicts themselves are the store's). */

import type { RoomVerdict, StoreApp } from "./models/Store";

export const VERDICT_LABELS: Record<RoomVerdict, string> = {
  FITS: "Fits",
  TIGHT: "Tight",
  WONT_FIT: "Won't fit",
};

export const VERDICT_NOTES: Record<RoomVerdict, string> = {
  FITS: "Plenty of room for it.",
  TIGHT: "It fits, but leaves little room for anything else.",
  WONT_FIT: "",
};

/** A size in MB as the page writes it: KB under one MB, GB to one decimal from 1024 MB. */
export function formatMb(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  if (mb < 1) return `${Math.max(1, Math.round(mb * 1024))} KB`;
  return `${Math.round(mb * 10) / 10} MB`;
}

/** What a build needs, as "3.0 GB of memory and 900 MB of disk"; null for an app that is not built. */
export function buildNeeds(buildMemoryMb: number | null, buildDiskMb: number | null): string | null {
  const needs = [
    buildMemoryMb === null ? null : `${formatMb(buildMemoryMb)} of memory`,
    buildDiskMb === null ? null : `${formatMb(buildDiskMb)} of disk`,
  ].filter((part) => part !== null);
  return needs.length === 0 ? null : needs.join(" and ");
}

/** What building an app needs, in one sentence; empty for an app that is not built. */
export function buildText(app: StoreApp): string {
  const needs = buildNeeds(app.build_memory_mb, app.build_disk_mb);
  if (needs === null) return "";
  return `Installing builds it from its source first, which needs about ${needs} for a minute or so. The fit counts that too.`;
}

/** The disk line for an app: the measured figure once installed, else the estimate, else that it is unknown. */
export function diskText(app: StoreApp): string {
  if (app.measured_disk_mb !== null) return `${formatMb(app.measured_disk_mb)} measured`;
  if (app.disk_mb === null) return "unknown";
  return app.is_added ? `about ${formatMb(app.disk_mb)}` : formatMb(app.disk_mb);
}

/** The memory line for an app: its measured peak once it has run, else the in-use estimate, else that it is unknown
 *  until it runs. */
export function memoryText(app: StoreApp): string {
  if (app.peak_memory_mb !== null) return `${formatMb(app.peak_memory_mb)} measured`;
  if (app.memory_in_use_mb !== null) return `${formatMb(app.memory_in_use_mb)} in use`;
  return "unknown until it runs";
}
