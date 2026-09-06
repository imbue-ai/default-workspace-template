/**
 * Messages that carry no information, in the spellings a stringified body takes.
 *
 * Mithril builds its rejection as `new Error(body)`, and `Error` stringifies
 * whatever it is handed -- so a body it could not read arrives as the *word*
 * "null", not as an absent message. (With `responseType: "json"` and a
 * non-JSON body, `xhr.response` is null and reading `xhr.responseText` throws,
 * which is exactly a proxy's plain-text 503.) An emptiness check alone lets
 * that through, which is how "Error: null" reached the user. A parsed object
 * body lands here as "[object Object]" the same way. ("undefined" is
 * unreachable from mithril itself -- `new Error(undefined).message` is "" --
 * and is listed only so any other producer of this shape is covered too.)
 */
const UNINFORMATIVE_MESSAGES: ReadonlySet<string> = new Set(["null", "undefined", "[object Object]"]);

/**
 * Extract a human-readable message from a failed `m.request` rejection.
 *
 * Mithril rejects with an Error-like value carrying (when available) the parsed
 * response body, an HTTP status `code`, and a `message`. A gateway error (e.g. a
 * 504 from a front-door proxy) often has no JSON body, so naive
 * `response.detail` extraction yields `null`/`undefined` and surfaces a useless
 * "null" to the user. This walks the available fields in order of usefulness and
 * always returns a non-empty string.
 */
export function describeRequestError(error: unknown): string {
  if (error === null || error === undefined) {
    return "unknown error";
  }
  if (typeof error === "string") {
    return error.trim() || "unknown error";
  }
  const err = error as { response?: { detail?: unknown } | null; message?: unknown; code?: unknown };

  const detail = err.response?.detail;
  if (typeof detail === "string" && detail.trim() !== "") {
    return detail.trim();
  }

  const message = typeof err.message === "string" ? err.message.trim() : "";
  if (message !== "" && !UNINFORMATIVE_MESSAGES.has(message)) {
    return message;
  }

  if (typeof err.code === "number" && err.code !== 0) {
    return `request failed (HTTP ${err.code})`;
  }

  // Code 0 is mithril's "no HTTP response at all": the request never reached a
  // server (connection refused, DNS failure, a tunnel that is down). There is no
  // status to name, but "unknown error" would hide the one thing that is known.
  if (err.code === 0) {
    return "could not reach the workspace";
  }

  return "unknown error";
}

/**
 * mngr's classification of a failed send, when it supplied one.
 *
 * The reason a send failed is written for a person and varies per harness, so it cannot be used
 * to decide whether trying again could possibly help. This is the machine-readable half. mngr
 * names the situation and stops there -- it does not know what a button is -- so the mapping from
 * a kind to what the user is offered lives here, in the workspace.
 *
 * Anything unrecognised (an older backend, a kind added later) reads as "unknown", which callers
 * treat exactly as they behaved before kinds existed.
 */
export type SendFailureKind = "input_blocked" | "not_ready" | "agent_unreachable" | "unknown";

const KNOWN_SEND_FAILURE_KINDS: ReadonlySet<string> = new Set(["input_blocked", "not_ready", "agent_unreachable"]);

export function describeRequestErrorKind(error: unknown): SendFailureKind {
  if (error === null || typeof error !== "object") {
    return "unknown";
  }
  const kind = (error as { response?: { kind?: unknown } | null }).response?.kind;
  return typeof kind === "string" && KNOWN_SEND_FAILURE_KINDS.has(kind) ? (kind as SendFailureKind) : "unknown";
}
