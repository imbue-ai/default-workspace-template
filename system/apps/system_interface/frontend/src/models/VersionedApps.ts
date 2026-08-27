/**
 * Which apps the Versioning app actually serves a timeline for.
 *
 * The versioning app versions FOLDERS under `system/apps`, while the shell
 * knows a service REGISTRY, and the two diverge in both directions: a port
 * registered with no package of its own behind it (`si-preview` while a preview
 * is up, mngr's `owner-exec`) is a service with no timeline, and the shell's own
 * folder has a timeline under a name nothing registers (`system-interface`).
 * So "does this name have a history" cannot be derived from the registry at
 * all; it is answered by the versioning app's own `GET /api/apps`, which is the
 * list it answers `/app/<name>` for, and this module is where that answer is
 * kept.
 *
 * The fetch goes through the shell's own backend (`GET /api/versioned-apps`)
 * rather than to the versioning origin directly: sibling service origins are
 * same-site but not same-origin, and the versioning app sends no CORS headers
 * -- the same server-side hop the browser fleet's API takes.
 *
 * Menus are built synchronously on the click that opens them, so the answer has
 * to be in hand before one does: it is fetched at startup and refreshed
 * alongside the machine inventory, at most once per TTL. A fetch that fails
 * leaves the last good answer in place (a stopped versioning service must not
 * cost the History rows of a workspace that had them a moment ago), and until
 * the first one lands the answer is `null` -- "not known yet", which every
 * caller reads as offering no row, since guessing is the thing this replaces.
 *
 * That leaves one window worth closing on its own: a shell loaded while the
 * versioning service is still starting has no answer AND no reason to ask
 * again, since the routine occasions are all things the user has to do (mount a
 * view, open a launcher, create a terminal). So a failure with nothing cached
 * -- and only that case -- schedules its own retry on the shared reconnect
 * backoff, a bounded handful of times, which is about as long as a supervised
 * service takes to come up. Once any answer lands the retries stop and the TTL
 * takes over; a service that is simply down stops being asked rather than being
 * polled forever.
 *
 * Kept apart from models/AgentManager (whose module state the view tests mock
 * wholesale) so views can import this without every mock having to restate it
 * -- the same reason models/AppInstances and models/appLiveness stand alone.
 */

import { apiUrl } from "../base-path";
import { ReconnectBackoff } from "./backoff";

/** How long a fetched list is treated as current. The list changes only when an
 *  app folder appears or goes -- rare, and never urgent -- so this is about
 *  bounding staleness rather than tracking a live value. */
const VERSIONED_APPS_TTL_MS = 60_000;

/** How many times a cold start retries on its own before leaving it to the
 *  routine occasions. On the shared backoff (1s doubling to a 30s cap) this
 *  spans about a minute -- long enough for a supervised service to come up,
 *  short enough that a workspace with no versioning service is not polled. */
const COLD_START_RETRY_LIMIT = 6;

// The last answer the versioning app gave, or null before the first one lands.
// Read synchronously by every menu surface on the click that builds it.
let versionedAppNames: ReadonlySet<string> | null = null;
// When that answer arrived, for the TTL. Never advanced by a failed fetch, so a
// failure retries on the next occasion rather than pinning a stale answer.
let fetchedAtMs = 0;
// The fetch in flight, so concurrent callers share one request.
let inFlight: Promise<void> | null = null;
// The cold-start retry: its pending timer, how many it has spent, and the delay
// schedule it shares with the WebSocket and SSE reconnects.
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let retriesSpent = 0;
let retryBackoff = new ReconnectBackoff();

/** The names the versioning app serves a timeline for, or null before the first
 *  successful fetch. A name absent from the set has no history to open. */
export function getVersionedAppNames(): ReadonlySet<string> | null {
  return versionedAppNames;
}

/** Ask the backend for the versioning app's list. Defensive like
 *  `fetchAppInstances`: an unreachable versioning service (or shell) yields
 *  null, which the caller reads as "keep what we had" rather than "nothing has
 *  a history". */
export async function fetchVersionedAppNames(): Promise<ReadonlySet<string> | null> {
  try {
    const response = await fetch(apiUrl("/api/versioned-apps"));
    if (!response.ok) return null;
    const data = (await response.json()) as { apps?: { name?: string }[] };
    if (!Array.isArray(data.apps)) return null;
    return new Set(data.apps.map((app) => app.name).filter((name): name is string => typeof name === "string"));
  } catch {
    return null;
  }
}

/** Re-read the list into the cache, sharing one request among concurrent
 *  callers. A failed fetch leaves the previous answer (and its age) untouched,
 *  and -- when there is no answer at all yet -- arranges its own retry. */
export async function refreshVersionedApps(): Promise<void> {
  if (inFlight !== null) return inFlight;
  inFlight = fetchVersionedAppNames()
    .then((names) => {
      if (names === null) {
        scheduleColdStartRetry();
        return;
      }
      versionedAppNames = names;
      fetchedAtMs = Date.now();
      cancelColdStartRetry();
    })
    .finally(() => {
      inFlight = null;
    });
  return inFlight;
}

/** Ask again shortly, but only while the shell has never had an answer -- the
 *  case where nothing else will ask until the user does something. A retry
 *  already pending, an answer already in hand, or a spent allowance all stand
 *  down. */
function scheduleColdStartRetry(): void {
  if (versionedAppNames !== null || retryTimer !== null || retriesSpent >= COLD_START_RETRY_LIMIT) return;
  retriesSpent += 1;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    void refreshVersionedApps();
  }, retryBackoff.nextDelay());
}

/** Stop retrying: an answer landed, so the TTL and the routine occasions own
 *  the list from here. The allowance is spent for good rather than reset -- a
 *  later failure keeps the answer that landed, which is not the cold start this
 *  exists for. */
function cancelColdStartRetry(): void {
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = null;
  retriesSpent = COLD_START_RETRY_LIMIT;
}

/** Refresh only if the cached answer is missing or older than the TTL. What
 *  every routine occasion (startup, a machine-inventory refresh) calls, so
 *  those occasions cost at most one request a minute. */
export async function ensureVersionedAppsFresh(): Promise<void> {
  if (versionedAppNames !== null && Date.now() - fetchedAtMs < VERSIONED_APPS_TTL_MS) return;
  await refreshVersionedApps();
}

/** Drop the cache and any pending retry. For tests, which must not inherit
 *  another test's answer -- nor leave a timer running into the next one. */
export function resetVersionedAppsForTesting(): void {
  versionedAppNames = null;
  fetchedAtMs = 0;
  inFlight = null;
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = null;
  retriesSpent = 0;
  retryBackoff = new ReconnectBackoff();
}
