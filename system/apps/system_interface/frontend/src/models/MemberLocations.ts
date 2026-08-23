/**
 * Where each beaconing object was last looking, keyed by ref.
 *
 * An app instance that posts its location (the vendored file viewer beacons
 * the folder it is showing on each page load -- see the shell's beacon
 * listener in src/locationBeacon.ts) reopens there: the pane's URL is the
 * service origin plus the stored path. The location is a fact about the **object**,
 * machine-wide, exactly as a name (MemberTitles) and a recency
 * (MemberLastUsed) are, and this module mirrors those two: a sibling of
 * Projects, cached at startup, kept current by the
 * `member_location_changed` broadcast, read synchronously by the open paths.
 *
 * A ref that is absent has simply never beaconed, and its panes open at the
 * service origin as before anything was stored.
 */

import { apiUrl } from "../base-path";

/** Where each object was last looking, keyed by ref. */
export type MemberLocationMap = Readonly<Record<string, string>>;

// What the server last told us. Replaced wholesale by a load and patched by
// each broadcast; read synchronously by every open path.
let locationByRef: Record<string, string> = {};

/** The stored opening path for one object, or null when it has none. */
export function getMemberLocation(ref: string): string | null {
  const stored = locationByRef[ref];
  return stored === undefined || stored === "" ? null : stored;
}

/** Fetch the whole machine-wide map. Defensive like fetchMemberTitles: an
 *  unreachable server yields an empty map, which reads as "nothing has
 *  beaconed" rather than breaking any open. */
export async function fetchMemberLocations(): Promise<Record<string, string>> {
  try {
    const response = await fetch(apiUrl("/api/member-locations"));
    if (!response.ok) return {};
    const data = (await response.json()) as { locations?: Record<string, string> };
    return data.locations ?? {};
  } catch {
    return {};
  }
}

/** Load the map into the cache. Called once at startup, before the first view
 *  is mounted, so the first restore already opens instances where they were. */
export async function loadMemberLocations(): Promise<void> {
  locationByRef = await fetchMemberLocations();
}

/** Record what the server says one object's location is now; null means the
 *  entry was dropped (the object was destroyed, or it beaconed a blank). The
 *  `member_location_changed` broadcast lands here, and so does the response
 *  to our own beacon post. */
export function applyMemberLocationChange(ref: string, path: string | null): void {
  if (path === null || path === "") {
    delete locationByRef[ref];
    return;
  }
  locationByRef[ref] = path;
}

/**
 * Tell the server where one object is looking, fire-and-forget.
 *
 * The shell calls this after validating a beacon's origin and resolving the
 * posting pane to its ref. The cache is updated from the server's own answer
 * (the broadcast that follows repaints every other client); a failed POST is
 * simply dropped, because a location is an opening hint and the next beacon
 * records it again.
 */
export function recordMemberLocation(ref: string, path: string): void {
  void fetch(apiUrl("/api/member-locations"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref, path }),
  })
    .then(async (response) => {
      if (!response.ok) return;
      const data = (await response.json()) as { path?: string | null };
      applyMemberLocationChange(ref, data.path ?? null);
    })
    .catch(() => {
      // Fire-and-forget: an unreachable server must never break the page that
      // beaconed, and the map it serves back later is the authority anyway.
    });
}
