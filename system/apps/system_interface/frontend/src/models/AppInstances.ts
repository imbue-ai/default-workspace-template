/**
 * The machine's app instances, and the allocator that mints new ones.
 *
 * A plain app's instances are DERIVED, server-side, from what references them
 * -- every project's member list plus every view's saved layout -- so the
 * frontend cannot enumerate them from its own state (another view's saved
 * arrangement never reaches this client). The server serves the inventory
 * (`GET /api/apps/instances`) and this module caches it the way the projects
 * list is cached: fetched at startup, re-fetched whenever a broadcast says
 * references moved (membership, saves, panel removals -- the workspace calls
 * `refreshAppInstances` from those), and read synchronously by every surface
 * that lists instances.
 *
 * Kept apart from models/AgentManager (whose module state the view tests mock
 * wholesale) so views can import these without every mock having to restate
 * them -- the same reason models/appLiveness stands alone.
 */

import { apiUrl } from "../base-path";
import { instanceNumberFromName } from "./Projects";

/** The machine's instances, by registered service name, in number order. */
export type AppInstanceMap = Readonly<Record<string, readonly string[]>>;

// What the server last told us. Replaced wholesale by each refresh; read
// synchronously by the sidebar and the launcher on every redraw.
let instancesByService: Record<string, string[]> = {};

/** The cached inventory. A service that is absent simply has no instances. */
export function getAppInstances(): AppInstanceMap {
  return instancesByService;
}

/** The cached instance names of one service, possibly empty. */
export function instancesOfService(serviceName: string): readonly string[] {
  return instancesByService[serviceName] ?? [];
}

/** Fetch the whole machine-wide inventory. Defensive like fetchProjectsList:
 *  an unreachable server yields an empty map, which reads as "no instances"
 *  rather than breaking the sidebar. */
export async function fetchAppInstances(): Promise<Record<string, string[]>> {
  try {
    const response = await fetch(apiUrl("/api/apps/instances"));
    if (!response.ok) return {};
    const data = (await response.json()) as { instances?: Record<string, string[]> };
    return data.instances ?? {};
  } catch {
    return {};
  }
}

/** Re-read the inventory into the cache. Called at startup and whenever a
 *  broadcast says references moved; callers redraw afterwards. */
export async function refreshAppInstances(): Promise<void> {
  instancesByService = await fetchAppInstances();
}

/**
 * Ask the backend to mint the next free `<service>-<N>` instance name.
 *
 * The instance does not exist until something references it -- the open that
 * follows files it -- so the backend holds the name in an in-flight
 * reservation set, and concurrent mints get distinct names. Throws with the
 * server's detail on rejection (an unregistered service).
 */
export async function allocateAppInstance(serviceName: string): Promise<string> {
  const response = await fetch(apiUrl(`/api/apps/${encodeURIComponent(serviceName)}/instances/allocate`), {
    method: "POST",
  });
  if (!response.ok) {
    const data = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(data.detail ?? `HTTP ${response.status}`);
  }
  const data = (await response.json()) as { instance?: string };
  if (!data.instance) {
    throw new Error("Instance allocation returned no name");
  }
  return data.instance;
}

/**
 * The display name an instance derives from its identity: the app's own
 * display name plus the instance number -- "File Viewer 2", or "Docs 2" for
 * an app renamed to "Docs". `serviceLabel` is what the app itself is called
 * (the caller resolves it through the title store, which this module must not
 * import); a name that does not parse as `<service>-<N>` renders as itself,
 * prefixed, so a hand-edited ref still says what it belongs to.
 */
export function appInstanceDisplayName(serviceLabel: string, instanceName: string): string {
  const instanceNumber = instanceNumberFromName(instanceName);
  return instanceNumber === null ? `${serviceLabel} ${instanceName}` : `${serviceLabel} ${instanceNumber}`;
}
