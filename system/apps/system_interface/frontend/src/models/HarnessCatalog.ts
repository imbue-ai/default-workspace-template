/**
 * The static, per-harness model catalog -- the model bar's compile-time half.
 *
 * Fetched once from `GET /api/harnesses` and cached by harness. Holds everything
 * the bar needs that does not vary per agent: the selectable models (and which
 * efforts each declares, which are shown) and the switch mode. The per-agent live
 * selection arrives separately, on the agents WebSocket as each agent's
 * `model_choice` (see AgentManager.ts). The backend already computes which catalog
 * option a live choice matched, so the frontend never re-matches. The harness's
 * "Powered by" credit is fetched per agent (see PoweredByCredit.ts), not from here.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export interface EffortChoice {
  level: string;
  in_picker: boolean;
}

export interface CatalogModelOption {
  id: string;
  label: string;
  efforts: EffortChoice[];
  supports_fast: boolean;
  in_picker: boolean;
}

export interface HarnessCatalog {
  options: CatalogModelOption[];
  default_model_id: string;
  switch_mode: string; // "eager_then_reconcile" | "on_change" | "read_only"
  picker_mode: string; // "list" | "search" -- how the model dropdown renders (orthogonal to switch_mode)
}

const catalogByHarness = new Map<string, HarnessCatalog>();
let hasLoaded = false;
let isLoading = false;

/** The catalog for a harness, or null when unknown / not yet loaded. */
export function getHarnessCatalog(harness: string | undefined): HarnessCatalog | null {
  if (!harness) {
    return null;
  }
  return catalogByHarness.get(harness) ?? null;
}

/** Load the catalogs once (idempotent). Safe to call on every agent switch. */
export async function ensureHarnessCatalogs(): Promise<void> {
  if (hasLoaded || isLoading) {
    return;
  }
  isLoading = true;
  try {
    const data = await m.request<Record<string, HarnessCatalog>>({
      method: "GET",
      url: apiUrl("/api/harnesses"),
    });
    for (const [harness, catalog] of Object.entries(data)) {
      catalogByHarness.set(harness, catalog);
    }
    hasLoaded = true;
    m.redraw();
  } catch (error) {
    console.warn("Failed to load harness catalogs", error);
  } finally {
    isLoading = false;
  }
}

/** Bare alias of a model string, matching the backend's `base_alias`
 *  (`opus[1m]` -> `opus`), so a stored `opus` or `opus[1m]` both map to one option. */
export function baseAlias(model: string): string {
  return model.split("[")[0].trim().toLowerCase();
}

/** The catalog option a model id resolves to, by bare alias, or null. */
export function findOption(catalog: HarnessCatalog, modelId: string): CatalogModelOption | null {
  const alias = baseAlias(modelId);
  return catalog.options.find((option) => baseAlias(option.id) === alias) ?? null;
}
