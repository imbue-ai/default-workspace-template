/**
 * Creating a browser in the per-workspace fleet.
 *
 * Browsers are addressed by NAME everywhere (not a numeric id): the CLI
 * ``<name>`` arg, the ``service:browser?session=<name>`` ref, the cast
 * WebSocket ``/browsers/<name>/cast``, the manifest, and the on-disk profile
 * dir all key off the name. The name is machine-minted by the create flow (the
 * user never types one -- the display name they see is the auto-filed
 * "Browser N" in the member-titles store); the daemon rejects invalid names
 * (400) and duplicates / a full fleet (409), and the caller surfaces the
 * daemon's reason so a failure is never silent.
 */

import { apiUrl } from "../base-path";

// Mirrors the daemon's ``names.is_valid_browser_name``: lowercase alphanumeric
// words joined by single dashes, 1..40 chars, no leading/trailing/double dash.
// Kept here (not the regex inline) so the rule reads the same as the Python one.
const BROWSER_NAME_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const MAX_BROWSER_NAME_LEN = 40;

// Validate a name against the daemon's rule. Returns ``null`` when valid, or a
// short error message explaining what is wrong. Pure-numeric names are rejected
// (the daemon rejects them too, so an upgraded workspace's old numeric profile
// dirs never resurrect as named browsers). The create flow uses this as a guard
// on the machine-minted name; the daemon still validates authoritatively.
export function validateBrowserName(name: string): string | null {
  if (!name) {
    return "Enter a browser name.";
  }
  if (name.length > MAX_BROWSER_NAME_LEN) {
    return `Name must be at most ${MAX_BROWSER_NAME_LEN} characters.`;
  }
  if (/^[0-9]+$/.test(name)) {
    return "Name cannot be only digits.";
  }
  if (!BROWSER_NAME_RE.test(name)) {
    return "Use lowercase letters, numbers, and single dashes (e.g. alex-smith).";
  }
  return null;
}

/** What one create attempt came to: the daemon's final name on success, or the
 *  reason it refused -- carried out rather than thrown, because the caller has
 *  already opened an optimistic pane and needs to know which branch to walk. */
export interface BrowserCreateResult {
  ok: boolean;
  // The daemon's final chosen name on success (equal to the requested one in
  // practice); the requested name on failure.
  name: string;
  // Why the create failed, "" on success. The daemon's ``error`` body for a
  // 400/409/503, or a network message when the POST never reached it.
  reason: string;
}

/**
 * Register browser ``name`` with the daemon.
 *
 * The registration returns fast (the Chromium launch runs serialized in the
 * background and the viewer watches it flip from ``init`` to ``running`` over
 * the cast socket), which is what lets the caller open the optimistic
 * "Starting browser..." pane before this resolves. Goes through the
 * same-origin backend passthrough because the daemon itself lives on a sibling
 * service origin. Never throws: a failure comes back as ``{ok: false}`` with
 * the reason the caller must surface.
 */
export async function createBrowser(name: string): Promise<BrowserCreateResult> {
  let response: Response;
  try {
    response = await fetch(apiUrl("/api/browsers"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  } catch {
    return { ok: false, name, reason: "Could not reach the browser service. Check your connection and try again." };
  }
  const data = (await response.json().catch(() => ({}))) as { name?: string; error?: string };
  if (response.ok) {
    return { ok: true, name: typeof data.name === "string" ? data.name : name, reason: "" };
  }
  // 400 invalid / 409 duplicate-or-full / 503 installing: surface the daemon's
  // reason verbatim, falling back to a generic line so it is never blank.
  const reason =
    typeof data.error === "string" && data.error.trim() ? data.error : "The browser could not be created.";
  return { ok: false, name, reason };
}
