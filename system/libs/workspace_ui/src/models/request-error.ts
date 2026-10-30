import { failureText, readFailure, type Failure, type RequestKind } from "./failure";
import { HttpError } from "./http";

/**
 * Messages that carry no information, in the spellings a stringified body takes.
 *
 * Mithril builds its rejection as `new Error(body)`, and `Error` stringifies
 * whatever it is handed -- so a body it could not read arrives as the *word*
 * "null", not as an absent message. (With `responseType: "json"` and a
 * non-JSON body, `xhr.response` is null and reading `xhr.responseText` throws,
 * which is exactly a proxy's plain-text 503.) An emptiness check alone lets
 * that through. A parsed object body lands here as "[object Object]" the same
 * way. ("undefined" is
 * unreachable from mithril itself -- `new Error(undefined).message` is "" --
 * and is listed only so any other producer of this shape is covered too.)
 */
const UNINFORMATIVE_MESSAGES: ReadonlySet<string> = new Set(["null", "undefined", "[object Object]"]);

function informativeMessage(value: unknown): string | null {
  const message = typeof value === "string" ? value.trim() : "";
  return message !== "" && !UNINFORMATIVE_MESSAGES.has(message) ? message : null;
}

/** A failure no request answered with: an error the code threw, whose message is developer text. */
function thrownFailure(message: string | null, requestKind: RequestKind): Failure {
  const fields = { requestKind, status: null, details: message, kind: null, body: null };
  return message === null ? { ...fields, cause: "no_reason" } : { ...fields, cause: "reason", reason: message };
}

/**
 * Read a failed request -- a mithril `m.request` rejection, an ``HttpError``, or an error a caller threw -- the way
 * ``readFailure`` reads a REST answer. ``requestKind`` says whether the request asked for a change; an ``HttpError``
 * already knows from its method, which a caller's ``requestKind`` overrides (a POST that only reads).
 *
 * A mithril rejection carries (when available) the parsed response body, the HTTP status as `code` (0 when nothing
 * answered), and a `message` that is the raw body text.
 */
export function describeRequestFailure(error: unknown, requestKind?: RequestKind): Failure {
  if (error instanceof HttpError) {
    return requestKind === undefined ? error.failure : { ...error.failure, requestKind };
  }
  const kind = requestKind ?? "change";
  if (error === null || error === undefined) return thrownFailure(null, kind);
  if (typeof error === "string") return thrownFailure(informativeMessage(error), kind);
  const err = error as { response?: unknown; message?: unknown; code?: unknown };
  const message = informativeMessage(err.message);
  if (typeof err.code !== "number") return thrownFailure(message, kind);
  return readFailure({ status: err.code, body: err.response, rawText: message, requestKind: kind });
}

/** ``failureText`` for ``error``: the server's own words, for a surface not yet worded with ``failureSentence``;
 *  always non-empty. */
export function describeRequestError(error: unknown, requestKind?: RequestKind): string {
  return failureText(describeRequestFailure(error, requestKind));
}

/**
 * mngr's classification of a failed send, when it supplied one.
 *
 * The reason a send failed is written for a person and varies per harness, so it cannot be used
 * to decide whether trying again could possibly help. This is the machine-readable half. mngr
 * names the situation and stops there -- it does not know what a button is -- so the mapping from
 * a kind to what the user is offered lives here, in the workspace.
 *
 * Anything unrecognised (an older backend, a kind added later) reads as "unknown", for which
 * callers offer nothing kind-specific.
 */
export type SendFailureKind =
  "input_blocked" | "not_ready" | "agent_unreachable" | "account_signed_out" | "chat_gone" | "unknown";

const KNOWN_SEND_FAILURE_KINDS: ReadonlySet<string> = new Set([
  "input_blocked",
  "not_ready",
  "agent_unreachable",
  "account_signed_out",
  "chat_gone",
]);

export function describeRequestErrorKind(error: unknown): SendFailureKind {
  if (error === null || typeof error !== "object") {
    return "unknown";
  }
  const kind = describeRequestFailure(error).kind;
  return kind !== null && KNOWN_SEND_FAILURE_KINDS.has(kind) ? (kind as SendFailureKind) : "unknown";
}
