/**
 * The models an account can run an agent on: what the switch dialog's picker offers for the agent a
 * switch leaves the chat on (a handoff's successor, or a rebind's agent restarted on the account), or
 * for a new chat.
 *
 * Read from ``GET /api/accounts/:id/model-options``, the account-level twin of the per-chat
 * ``/model-options``: a static harness offers its catalog, a dynamic one (codex) the options an agent
 * of the account was last offered (empty when the account has run none). Unlike the per-chat twin,
 * this route names no model ids of its own -- the agent the pick is for is not on the account yet, so
 * it has no set that could narrow the catalog.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import type { CatalogModelOption } from "./HarnessCatalog";
import { ensureHarnessCatalogs, getHarnessCatalog } from "./HarnessCatalog";
import { accountForAgent } from "./Providers";

interface AccountModelOptionsResponse {
  options?: CatalogModelOption[] | null;
}

/** The pickable options for an agent on ``accountId``, in catalog order; empty when nothing is known, and
 *  for a harness whose model the chat app cannot switch (agy's is changed from the agent's terminal). */
export async function fetchAccountModelOptions(accountId: string): Promise<CatalogModelOption[]> {
  await ensureHarnessCatalogs();
  const catalog = getHarnessCatalog(accountForAgent(accountId)?.harness);
  if (catalog?.switch_mode === "read_only") return [];
  const response = await m.request<AccountModelOptionsResponse>({
    method: "GET",
    url: apiUrl("/api/accounts/:accountId/model-options"),
    params: { accountId },
  });
  const options = response.options ?? null;
  if (options !== null) return options.filter((option) => option.in_picker);
  return (catalog?.options ?? []).filter((option) => option.in_picker);
}
