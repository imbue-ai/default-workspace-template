/** What the workspace's REST routes answer, read the way the user needs it said. */

import { failureText, readFailure, type Failure, type RequestKind } from "./failure";

/**
 * A refused or unanswered request to a REST route, as ``readFailure`` read it. Its ``message`` is ``failureText``'s
 * (the server's reason, for the log and the screens not yet worded); a surface words it with ``failureSentence``.
 */
export class HttpError extends Error {
  constructor(readonly failure: Failure) {
    super(failureText(failure));
  }

  /** The HTTP status, or 0 when nothing answered. */
  get status(): number {
    return this.failure.status ?? 0;
  }

  /** The JSON object the server answered, for a caller that reads a field of its own; null when there was none. */
  get body(): Readonly<Record<string, unknown>> | null {
    return this.failure.body;
  }
}

/** The failure a non-2xx ``response`` stands for. */
export async function readResponseFailure(response: Response, requestKind: RequestKind): Promise<HttpError> {
  const text = await response.text().catch(() => "");
  let body: unknown = null;
  try {
    body = JSON.parse(text);
  } catch {
    // Not JSON (an error page, a proxy's plain text): quoted behind "See details".
  }
  return new HttpError(readFailure({ status: response.status, body, rawText: text, requestKind }));
}

/** ``fetch`` that answers a request nothing answered (the connection refused or dropped) as an ``HttpError``. */
export async function fetchOrNotAnswered(url: string, init: RequestInit, requestKind: RequestKind): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch (error) {
    throw new HttpError(readFailure({ status: 0, body: null, rawText: (error as Error).message, requestKind }));
  }
}

/**
 * A JSON API call: the method, a JSON body when one is given, and the parsed JSON reply (nothing for a 204),
 * throwing an ``HttpError`` on a refusal or no answer. A GET is a read, any other method a change. Never served from
 * the browser's cache: an API answer is read for what the server says now.
 */
export async function requestJson<T>(method: string, url: string, body?: unknown): Promise<T> {
  const requestKind: RequestKind = method === "GET" ? "read" : "change";
  const response = await fetchOrNotAnswered(
    url,
    {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    },
    requestKind,
  );
  if (!response.ok) throw await readResponseFailure(response, requestKind);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** GET ``url`` and answer the parsed JSON reply, throwing an ``HttpError`` on a refusal or no answer. */
export function getJson<T>(url: string): Promise<T> {
  return requestJson<T>("GET", url);
}

/** POST a JSON body and answer the parsed JSON reply, throwing an ``HttpError`` on a refusal or no answer. */
export function postJson<T>(url: string, body: unknown): Promise<T> {
  return requestJson<T>("POST", url, body);
}
