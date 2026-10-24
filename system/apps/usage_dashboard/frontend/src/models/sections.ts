/**
 * The details, one section per provider in use, matching the cards at the top: a provider's subscription usage
 * and its usage without a subscription are separate sections, since one is limits and the other is spend.
 *
 * Every detail query is grouped by ``provider`` and ``billing_mode`` as well, so a section's rows are picked out
 * of the same answers the Total row adds up. Percentiles do not add up across rows, so a section's latency is
 * asked for on its own, filtered to its provider and billing modes.
 */

import type { Group } from "./api";
import { providerName, subscriptionTitle } from "./providers";

export type SectionKind = "subscription" | "spend";

export interface SectionSpec {
  key: string;
  kind: SectionKind;
  provider: string;
  title: string;
  /** The billing modes the section's calls are on. */
  billingModes: string[];
}

/** The fields every detail query adds, so rows can be split into sections. */
export const SECTION_FIELDS = ["provider", "billing_mode"] as const;

export function sectionKindOf(billingMode: string | null): SectionKind {
  return billingMode === "SUBSCRIPTION" ? "subscription" : "spend";
}

export function sectionKeyOf(group: Group): string {
  return `${sectionKindOf(group.billing_mode ?? null)}:${group.provider ?? ""}`;
}

/** The sections the rows have calls in: subscriptions first, then the rest, each by calls, most first. */
export function detailSections(rows: readonly { group: Group; calls: number }[]): SectionSpec[] {
  const sections = new Map<string, SectionSpec & { calls: number }>();
  for (const row of rows) {
    if (row.calls === 0) continue;
    const key = sectionKeyOf(row.group);
    const provider = row.group.provider ?? "";
    const kind = sectionKindOf(row.group.billing_mode ?? null);
    const section = sections.get(key) ?? {
      key,
      kind,
      provider,
      title: kind === "subscription" ? subscriptionTitle(provider) : `${providerName(provider || "unknown")} API`,
      billingModes: [],
      calls: 0,
    };
    const mode = row.group.billing_mode ?? "";
    if (!section.billingModes.includes(mode)) section.billingModes.push(mode);
    section.calls += row.calls;
    sections.set(key, section);
  }
  return [...sections.values()]
    .sort((a, b) => (a.kind === b.kind ? b.calls - a.calls : a.kind === "subscription" ? -1 : 1))
    .map(({ calls: _calls, ...section }) => section);
}

export function inSection(group: Group, section: SectionSpec): boolean {
  return sectionKeyOf(group) === section.key;
}
