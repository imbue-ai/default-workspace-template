/**
 * The pages, one document reading its path: the home page at ``/``, all usage at ``/all``, and one provider's
 * usage at ``/p/<item>``, where the item is a section key (``subscription:anthropic``, ``spend:openai``).
 */

import type { Group } from "./api";
import { sectionKeyOf } from "./sections";

export type Route = { page: "home" } | { page: "all" } | { page: "item"; key: string };

export function routeOf(path: string): Route {
  if (path === "/all") return { page: "all" };
  const match = /^\/p\/([^/]+)$/.exec(path);
  if (match !== null) return { page: "item", key: decodeURIComponent(match[1]) };
  return { page: "home" };
}

export function pathOf(route: Route): string {
  switch (route.page) {
    case "home":
      return "/";
    case "all":
      return "/all";
    case "item":
      return `/p/${encodeURIComponent(route.key)}`;
  }
}

/** Every billing mode that is not a subscription's, which a spend item's calls are on. */
export const PAY_AS_YOU_GO_MODES = ["API_KEY", "IMBUE_KEY", "UNKNOWN", ""] as const;

/** The call filters that keep only an item's calls: its provider, and its billing modes. */
export function scopeFilters(route: Route): Record<string, readonly string[]> {
  if (route.page !== "item") return {};
  const [kind, provider] = route.key.split(":", 2);
  return {
    provider: [provider ?? ""],
    billing_mode: kind === "subscription" ? ["SUBSCRIPTION"] : PAY_AS_YOU_GO_MODES,
  };
}

export function inScope(group: Group, route: Route): boolean {
  return route.page !== "item" || sectionKeyOf(group) === route.key;
}

/** ``scope`` narrowed by the page's filter: where both name a field, only the values both allow. */
export function combinedFilters(
  scope: Record<string, readonly string[]>,
  filter: Record<string, string> | null,
): Record<string, readonly string[]> {
  const combined: Record<string, readonly string[]> = { ...scope };
  for (const [field, value] of Object.entries(filter ?? {})) {
    const allowed = scope[field];
    combined[field] = allowed === undefined || allowed.includes(value) ? [value] : [];
  }
  return combined;
}
