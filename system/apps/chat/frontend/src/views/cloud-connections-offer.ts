/**
 * The permission card's offer of the account's own cloud connections: whether
 * the backend offers them for a request's scope on this chat, and taking the
 * offer.
 *
 * The first question for a (chat, scope) pair kicks off a fetch and returns
 * null; when it lands the answer is cached and the card redrawn. Any answer is
 * definitive for the page: the offer is a hint, so a failed lookup is no offer
 * rather than something to retry. Taking the offer restarts the chat's agent,
 * so the request runs for as long as that takes; once it succeeds the chat's
 * agent loads the connections, and every offer this page cached for the chat
 * is dropped.
 */
import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export interface CloudConnectionsOffer {
  is_offered: boolean;
  provider_name: string | null;
}

const NO_OFFER: CloudConnectionsOffer = { is_offered: false, provider_name: null };

type CacheEntry = { state: "loading" } | { state: "ready"; offer: CloudConnectionsOffer };

const cache = new Map<string, CacheEntry>();
// The requests whose offer this page took, so their cards flip before the
// notice's transcript round trip.
const takenRequestIds = new Set<string>();

function cacheKey(chatId: string, scope: string): string {
  return `${chatId}\n${scope}`;
}

async function fetchOffer(chatId: string, scope: string): Promise<CloudConnectionsOffer> {
  try {
    const path = `/api/chats/${encodeURIComponent(chatId)}/cloud-connections?scope=${encodeURIComponent(scope)}`;
    const response = await fetch(apiUrl(path));
    if (response.ok) {
      const body = (await response.json()) as Partial<CloudConnectionsOffer>;
      if (body.is_offered === true && typeof body.provider_name === "string") {
        return { is_offered: true, provider_name: body.provider_name };
      }
    }
  } catch {
    // Network-level failure: no offer.
  }
  return NO_OFFER;
}

/** The offer for a request's scope on a chat, or null while it is not known (or
 *  not made). The first call for a pair starts a one-time background fetch that
 *  redraws when it resolves. */
export function getCloudConnectionsOffer(chatId: string, scope: string): CloudConnectionsOffer | null {
  const key = cacheKey(chatId, scope);
  const cached = cache.get(key);
  if (cached === undefined) {
    cache.set(key, { state: "loading" });
    void fetchOffer(chatId, scope).then((offer) => {
      cache.set(key, { state: "ready", offer });
      m.redraw();
    });
    return null;
  }
  return cached.state === "ready" && cached.offer.is_offered ? cached.offer : null;
}

/** Whether this page took the offer made on a request. */
export function isCloudConnectionsOfferTaken(requestId: string): boolean {
  return takenRequestIds.has(requestId);
}

/** Take the offer made on a request: the chat's agent is set to load its
 *  account's cloud connections and restarted. */
export async function enableCloudConnections(
  chatId: string,
  requestId: string,
): Promise<{ ok: boolean; detail: string }> {
  try {
    const response = await fetch(apiUrl(`/api/chats/${encodeURIComponent(chatId)}/cloud-connections/enable`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId }),
    });
    if (response.ok) {
      takenRequestIds.add(requestId);
      for (const key of [...cache.keys()]) {
        if (key.startsWith(`${chatId}\n`)) cache.set(key, { state: "ready", offer: NO_OFFER });
      }
      return { ok: true, detail: "" };
    }
    let detail = `The chat app answered HTTP ${response.status}.`;
    try {
      const parsed = (await response.json()) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      // A non-JSON error body keeps the status-code wording.
    }
    return { ok: false, detail };
  } catch {
    return { ok: false, detail: "The chat app could not be reached." };
  }
}

/** Seed the cache directly. Only for tests and the dev-only visual preview,
 *  which have no backend to fetch from. */
export function seedCloudConnectionsOffer(chatId: string, scope: string, offer: CloudConnectionsOffer): void {
  cache.set(cacheKey(chatId, scope), { state: "ready", offer });
}

/** Drop every cached offer and taken request so the next test starts from a quiet page. */
export function resetCloudConnectionsOffersForTesting(): void {
  cache.clear();
  takenRequestIds.clear();
}
