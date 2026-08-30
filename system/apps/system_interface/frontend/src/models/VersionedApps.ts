import { apiUrl } from "../base-path";
import { ReconnectBackoff } from "./backoff";

const VERSIONED_APPS_TTL_MS = 60_000;

const COLD_START_RETRY_LIMIT = 6;

let versionedAppNames: ReadonlySet<string> | null = null;
let fetchedAtMs = 0;
let inFlight: Promise<void> | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let retriesSpent = 0;
let retryBackoff = new ReconnectBackoff();

export function getVersionedAppNames(): ReadonlySet<string> | null {
  return versionedAppNames;
}

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

function scheduleColdStartRetry(): void {
  if (versionedAppNames !== null || retryTimer !== null || retriesSpent >= COLD_START_RETRY_LIMIT) return;
  retriesSpent += 1;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    void refreshVersionedApps();
  }, retryBackoff.nextDelay());
}

function cancelColdStartRetry(): void {
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = null;
  retriesSpent = COLD_START_RETRY_LIMIT;
}

export async function ensureVersionedAppsFresh(): Promise<void> {
  if (versionedAppNames !== null && Date.now() - fetchedAtMs < VERSIONED_APPS_TTL_MS) return;
  await refreshVersionedApps();
}

export function resetVersionedAppsForTesting(): void {
  versionedAppNames = null;
  fetchedAtMs = 0;
  inFlight = null;
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = null;
  retriesSpent = 0;
  retryBackoff = new ReconnectBackoff();
}
