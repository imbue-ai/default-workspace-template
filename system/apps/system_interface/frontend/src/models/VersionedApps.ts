/**
 * Which apps the Versioning app actually serves a timeline for.
 *
 * The registry cannot answer that -- it diverges from the versioned folders in
 * both directions (`si-preview`/`owner-exec` are registered with no package;
 * the shell's folder is versioned under a name nothing registers) -- so this
 * caches the versioning app's own `GET /api/apps`, reached through the shell's
 * `/api/versioned-apps` (sibling origins are same-site but not same-origin).
 *
 * Menus read the answer synchronously, so it is fetched at startup and
 * refreshed alongside the machine inventory, at most once per TTL. A failed
 * fetch keeps the last good answer; `null` means "not known yet" and every
 * caller reads it as offering no row. A failure with nothing cached (shell
 * loaded before the versioning service came up) schedules a bounded retry on
 * the shared reconnect backoff; once any answer lands, the TTL takes over.
 *
 * Kept apart from models/AgentManager so views can import this without every
 * mock restating it -- same reason models/AppInstances stands alone.
 */

import { apiUrl } from "../base-path";
import { ReconnectBackoff } from "./backoff";

// The list changes only when an app folder appears or goes -- rare and never
// urgent -- so the TTL bounds staleness rather than tracking a live value.
const VERSIONED_APPS_TTL_MS = 60_000;

// On the shared backoff (1s doubling to a 30s cap) this spans about a minute:
// long enough for a supervised service to come up, short enough that a
// workspace with no versioning service is not polled forever.
const COLD_START_RETRY_LIMIT = 6;

// The last answer, or null before the first one lands.
let versionedAppNames: ReadonlySet<string> | null = null;
// When it arrived, for the TTL. Never advanced by a failed fetch, so a failure
// retries on the next occasion rather than pinning a stale answer.
let fetchedAtMs = 0;
// The fetch in flight, so concurrent callers share one request.
let inFlight: Promise<void> | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let retriesSpent = 0;
let retryBackoff = new ReconnectBackoff();

/** The names the versioning app serves a timeline for, or null before the first
 *  successful fetch. A name absent from the set has no history to open. */
export function getVersionedAppNames(): ReadonlySet<string> | null {
  return versionedAppNames;
}

/** Ask the backend for the versioning app's list. An unreachable service (or a
 *  malformed body) yields null -- "no answer", never "nothing has a history". */
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

/** Re-read the list into the cache. A failed fetch leaves the previous answer
 *  (and its age) untouched, and with nothing cached yet arranges its own retry. */
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
 *  case where nothing else will ask until the user does something. */
function scheduleColdStartRetry(): void {
  if (versionedAppNames !== null || retryTimer !== null || retriesSpent >= COLD_START_RETRY_LIMIT) return;
  retriesSpent += 1;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    void refreshVersionedApps();
  }, retryBackoff.nextDelay());
}

/** Stop retrying for good: an answer landed, so the TTL owns the list from
 *  here -- a later failure keeps that answer, which is not a cold start. */
function cancelColdStartRetry(): void {
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = null;
  retriesSpent = COLD_START_RETRY_LIMIT;
}

/** Refresh only if the cached answer is missing or older than the TTL -- what
 *  every routine occasion calls, so they cost at most one request per TTL. */
export async function ensureVersionedAppsFresh(): Promise<void> {
  if (versionedAppNames !== null && Date.now() - fetchedAtMs < VERSIONED_APPS_TTL_MS) return;
  await refreshVersionedApps();
}

/** Drop the cache and any pending retry, so tests do not leak into each other. */
export function resetVersionedAppsForTesting(): void {
  versionedAppNames = null;
  fetchedAtMs = 0;
  inFlight = null;
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = null;
  retriesSpent = 0;
  retryBackoff = new ReconnectBackoff();
}
