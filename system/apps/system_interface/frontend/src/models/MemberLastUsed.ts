/**
 * When the user last had each of the machine's objects in front of them,
 * keyed by ref.
 *
 * The launcher orders its tables most-recently-used first, and recency is a
 * fact about the **object**, machine-wide, not about the tab showing it: an
 * object used moments ago in one project must rank the same in every other one,
 * and a backgrounded object -- running, just not docked -- has no panel to keep
 * a timestamp on. Keying by ref makes both of those go away, exactly as
 * MemberTitles does for names, which is the pattern this file mirrors: a
 * sibling of Projects (a recency belongs to the machine and a member list
 * belongs to a project), cached the way the projects list is -- fetched once at
 * startup and kept current by the ``member_last_used_changed`` broadcast.
 *
 * The server's clock is the authority. A touch sends only the ref and the
 * server stamps the moment itself, which kills the clock-skew question: every
 * entry in the map is stamped by the one clock that also serves the map back.
 *
 * Touches are throttled per ref (see ``shouldRecordTouch``): focus flapping
 * between two panes must not produce a write per click, so a ref this client
 * recorded within the last minute is simply not recorded again. The launcher's
 * column is coarse ("3m ago"), so a minute of slack is invisible there.
 */

import { apiUrl } from "../base-path";

/** When each object was last in front of the user, keyed by ref. */
export type MemberLastUsedMap = Readonly<Record<string, number>>;

// What the server last told us. Replaced wholesale by a load and patched by
// each broadcast; read synchronously by the launcher on every redraw.
let lastUsedMsByRef: Record<string, number> = {};

// When this client last POSTed a touch, per ref -- the throttle's memory.
// Deliberately separate from the cache above: the cache is what the server
// said, and this is what we said to the server.
let recordedAtMsByRef: Record<string, number> = {};

/** How long a ref's recorded touch suppresses the next one. */
export const TOUCH_THROTTLE_MS = 60_000;

/**
 * Whether a touch of ``ref`` at ``nowMs`` is worth telling the server about.
 *
 * The decision is per ref rather than "same as the last one recorded": focus
 * flapping between two panes alternates the ref on every click, so a
 * last-one-only memory would record every single flap. Keyed by ref, each pane
 * is recorded once and the flapping is silent until the throttle expires --
 * and a ref this client has never recorded is always worth recording.
 */
export function shouldRecordTouch(
  ref: string,
  nowMs: number,
  recordedAtMs: Readonly<Record<string, number>>,
): boolean {
  const lastRecordedMs = recordedAtMs[ref];
  if (lastRecordedMs === undefined) return true;
  return nowMs - lastRecordedMs >= TOUCH_THROTTLE_MS;
}

/** The cached map, for the launcher's recency column. */
export function getMemberLastUsed(): MemberLastUsedMap {
  return lastUsedMsByRef;
}

/** Fetch the whole machine-wide map. Defensive like fetchMemberTitles: an
 *  unreachable server yields an empty map, which reads as "nothing has been
 *  used" rather than breaking the launcher. */
export async function fetchMemberLastUsed(): Promise<Record<string, number>> {
  try {
    const response = await fetch(apiUrl("/api/member-last-used"));
    if (!response.ok) return {};
    const data = (await response.json()) as { last_used?: Record<string, number> };
    return data.last_used ?? {};
  } catch {
    return {};
  }
}

/** Load the map into the cache. Called once at startup, before the first view
 *  is mounted, so the first launcher already ranks by recency. The throttle's
 *  memory resets with it: nothing has been recorded by this load of the app. */
export async function loadMemberLastUsed(): Promise<void> {
  lastUsedMsByRef = await fetchMemberLastUsed();
  recordedAtMsByRef = {};
}

/** Record what the server says one object's recency is now; null means the
 *  object was destroyed and its entry dropped -- a reused ref must not rank on
 *  the strength of a dead one. The `member_last_used_changed` broadcast lands
 *  here, and so does the response to our own touch. */
export function applyMemberLastUsedChange(ref: string, atMs: number | null): void {
  if (atMs === null) {
    delete lastUsedMsByRef[ref];
    delete recordedAtMsByRef[ref];
    return;
  }
  lastUsedMsByRef[ref] = atMs;
}

/**
 * Tell the server one object is in front of the user, fire-and-forget.
 *
 * The body carries only the ref -- the server stamps the moment with its own
 * clock and answers with what it stored, which is folded into the cache (the
 * broadcast that follows repaints every other client). Throttled per ref (see
 * ``shouldRecordTouch``); a failed POST is simply dropped, because recency is
 * an ordering hint and the next touch past the throttle records it again.
 */
export function touchMemberLastUsed(ref: string, nowMs: number = Date.now()): void {
  if (!shouldRecordTouch(ref, nowMs, recordedAtMsByRef)) return;
  recordedAtMsByRef[ref] = nowMs;
  void fetch(apiUrl("/api/member-last-used"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref }),
  })
    .then(async (response) => {
      if (!response.ok) return;
      const data = (await response.json()) as { at_ms?: number | null };
      if (typeof data.at_ms === "number") applyMemberLastUsedChange(ref, data.at_ms);
    })
    .catch(() => {
      // Fire-and-forget: an unreachable server must never stop a pane from
      // focusing, and the map it serves back later is the authority anyway.
    });
}
