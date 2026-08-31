/**
 * Lazily fetches and caches the latchkey catalog info for a permission scope
 * (the human-readable service name + per-permission descriptions) from the
 * backend's `/api/latchkey/scopes/<scope>` proxy, so a permission-request card
 * can show the real service name instead of the raw scope.
 *
 * The first request for a scope kicks off a fetch and returns null; when the
 * fetch lands it caches the result and triggers a redraw so the card updates.
 * Only a definitive answer is cached forever: a 404 (no such catalog entry) or
 * a 503 (this backend has no gateway, e.g. the dev sandbox) caches null and the
 * card keeps showing the raw scope. A transient failure -- the gateway
 * restarting (502), a network blip -- is retried after a delay, so one bad
 * window does not pin every card to raw scope ids for the life of the page.
 */
import m from "mithril";
import { apiUrl } from "../base-path";

export interface PermissionInfo {
  name: string;
  description: string | null;
}

export interface ScopeInfo {
  scope: string;
  display_name: string;
  description: string | null;
  permissions: PermissionInfo[];
}

type CacheEntry =
  | { state: "loading" }
  | { state: "ready"; info: ScopeInfo | null }
  | { state: "failed"; retryAtMs: number };

const cache = new Map<string, CacheEntry>();

/** How long a transiently-failed lookup waits before the next render retries it. */
export const SCOPE_INFO_RETRY_DELAY_MS = 15_000;

/** The resolved scope info if it's loaded, else null. The first call for a
 *  scope starts a one-time background fetch that redraws when it resolves;
 *  a transiently-failed fetch is started again once its retry delay passes. */
export function getScopeInfo(scope: string): ScopeInfo | null {
  const cached = cache.get(scope);
  if (cached === undefined || (cached.state === "failed" && Date.now() >= cached.retryAtMs)) {
    cache.set(scope, { state: "loading" });
    void fetchScopeInfo(scope);
    return null;
  }
  return cached.state === "ready" ? cached.info : null;
}

async function fetchScopeInfo(scope: string): Promise<void> {
  const entry = await fetchCacheEntry(scope);
  cache.set(scope, entry);
  if (entry.state === "failed") {
    // An idle page may not render again on its own; wake one render at the
    // retry time so the refetch actually happens.
    setTimeout(() => m.redraw(), SCOPE_INFO_RETRY_DELAY_MS);
  }
  m.redraw();
}

async function fetchCacheEntry(scope: string): Promise<CacheEntry> {
  try {
    const response = await fetch(apiUrl(`/api/latchkey/scopes/${encodeURIComponent(scope)}`));
    if (response.ok) {
      return { state: "ready", info: (await response.json()) as ScopeInfo };
    }
    if (response.status === 404 || response.status === 503) {
      return { state: "ready", info: null };
    }
  } catch {
    // Network-level failure: fall through to the transient-failure entry.
  }
  return { state: "failed", retryAtMs: Date.now() + SCOPE_INFO_RETRY_DELAY_MS };
}

/** Seed the cache directly. Only for the dev-only visual mockup, which has no
 *  backend to fetch from. */
export function seedScopeInfo(info: ScopeInfo): void {
  cache.set(info.scope, { state: "ready", info });
}

/** Drop every cached lookup so the next test starts from a quiet page. */
export function resetScopeInfoCacheForTesting(): void {
  cache.clear();
}
