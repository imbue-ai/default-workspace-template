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
  // The raw model id the harness reports in its live state file (or null = same as `id`).
  // Matched on the backend; the frontend reconciles against the matched option, not this.
  harness_reported_model_id: string | null;
}

export interface HarnessCatalog {
  // The static catalog options. EMPTY for a "dynamic" picker (codex): its options are per-agent,
  // fetched from /model-options on open, not carried here.
  options: CatalogModelOption[];
  switch_mode: string; // "eager_then_reconcile" (claude/pi -- optimistic) | "on_change" (codex -- no overlay)
  picker_mode: string; // "list" | "search" | "dynamic" -- how the model dropdown sources/renders options
  // Whether the "Shoulder tap" button can flush the queue atomically (merge into the live
  // turn without a restart). True only for codex; false harnesses use the restart-based flush.
  native_atomic_shoulder_tap_possible: boolean;
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
