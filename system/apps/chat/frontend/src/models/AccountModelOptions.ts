/**
 * The models an account can run a NEW agent on: what the switch dialog's picker offers for the
 * successor a handoff creates, or for a new chat, before either agent exists.
 *
 * Read from ``GET /api/accounts/:id/model-options``, the account-level twin of the per-chat
 * ``/model-options``: a static harness offers its catalog, a dynamic one (codex) the options the
 * account's last agent was offered (empty when it has run none). Unlike the per-chat twin, this
 * route names no model ids of its own -- there is no agent yet whose set could narrow the catalog.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import type { CatalogModelOption } from "./HarnessCatalog";
import { ensureHarnessCatalogs, getHarnessCatalog } from "./HarnessCatalog";
import { accountForAgent } from "./Providers";

interface AccountModelOptionsResponse {
  options?: CatalogModelOption[] | null;
}

/** The pickable options for a new agent on ``accountId``, in catalog order; empty when nothing is known. */
export async function fetchAccountModelOptions(accountId: string): Promise<CatalogModelOption[]> {
  await ensureHarnessCatalogs();
  const response = await m.request<AccountModelOptionsResponse>({
    method: "GET",
    url: apiUrl("/api/accounts/:accountId/model-options"),
    params: { accountId },
  });
  const options = response.options ?? null;
  if (options !== null) return options.filter((option) => option.in_picker);
  const catalog = getHarnessCatalog(accountForAgent(accountId)?.harness);
  return (catalog?.options ?? []).filter((option) => option.in_picker);
}
