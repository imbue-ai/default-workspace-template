/**
 * Creating a browser in the per-workspace fleet.
 *
 * Browsers are addressed by NAME everywhere (not a numeric id): the CLI
 * ``<name>`` arg, the ``service:browser?session=<name>`` ref, the cast
 * WebSocket ``/browsers/<name>/cast``, the manifest, and the on-disk profile
 * dir all key off the name. The daemon mints it -- the first free
 * ``browser-<N>``, whose "Browser N" display form every surface derives (see
 * ``views/derived-names``) -- and rejects invalid names (400) and duplicates /
 * a full fleet (409); the caller surfaces the daemon's reason so a failure is
 * never silent.
 */

import { apiUrl } from "../base-path";

/** What one create attempt came to: the daemon's minted name on success, or
 *  the reason it refused -- carried out rather than thrown so the caller has
 *  one shape to branch on. */
export interface BrowserCreateResult {
  ok: boolean;
  // The daemon's minted name on success; "" on failure.
  name: string;
  // Why the create failed, "" on success. The daemon's ``error`` body for a
  // 400/409/503, or a network message when the POST never reached it.
  reason: string;
}

/**
 * Register a new browser with the daemon, which mints and returns its name.
 *
 * The registration returns fast: the multi-second Chromium launch runs
 * serialized in the background, and the pane the caller opens on the returned
 * name watches it flip from ``init`` to ``running`` over the cast socket. Goes
 * through the same-origin backend passthrough because the daemon itself lives
 * on a sibling service origin. Never throws: a failure comes back as
 * ``{ok: false}`` with the reason the caller must surface.
 */
export async function createBrowser(): Promise<BrowserCreateResult> {
  let response: Response;
  try {
    response = await fetch(apiUrl("/api/browsers"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  } catch {
    return {
      ok: false,
      name: "",
      reason: "Could not reach the browser service. Check your connection and try again.",
    };
  }
  const data = (await response.json().catch(() => ({}))) as { name?: string; error?: string };
  if (response.ok && typeof data.name === "string" && data.name !== "") {
    return { ok: true, name: data.name, reason: "" };
  }
  // 400 invalid / 409 duplicate-or-full / 503 installing: surface the daemon's
  // reason verbatim, falling back to a generic line so it is never blank.
  const reason =
    typeof data.error === "string" && data.error.trim() ? data.error : "The browser could not be created.";
  return { ok: false, name: "", reason };
}
