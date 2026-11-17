/** The service's own routes and its ``/api/ws`` event socket, with a reconnecting subscription. */

import { readFailure } from "@imbue/workspace-ui/src/models/failure";
import type { Failure, RequestKind } from "@imbue/workspace-ui/src/models/failure";
import type { ConnectInfo, NekoSessionAnswer, ScreenSizeAnswer, ScreenStateDocument, ServiceEvent } from "./state";

const RECONNECT_MS = 1500;

/** A route of the service refused or failed a request; ``detail`` is what its JSON body said, else the status, and
 *  ``failure`` the whole answer as the workspace's failure reader reads it. */
export class ServiceRequestError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly failure: Failure,
  ) {
    super(`the Screen service answered ${status}: ${detail}`);
    this.name = "ServiceRequestError";
  }
}

/** A refused or failed answer as a ``ServiceRequestError``. */
async function refusalError(response: Response, requestKind: RequestKind): Promise<ServiceRequestError> {
  const rawText = await response.text().catch(() => "");
  let body: unknown;
  try {
    body = JSON.parse(rawText);
  } catch {
    body = null;
  }
  const detail = body !== null && typeof body === "object" ? (body as { detail?: unknown }).detail : undefined;
  const failure = readFailure({ status: response.status, body, rawText, requestKind });
  return new ServiceRequestError(
    response.status,
    typeof detail === "string" && detail ? detail : `status ${response.status}`,
    failure,
  );
}

/** What went wrong with a request to the service: a refusal's answer, or nothing answering at all (a network error,
 *  the service down). */
export function failureOf(error: unknown, requestKind: RequestKind): Failure {
  if (error instanceof ServiceRequestError) return error.failure;
  return readFailure({
    status: 0,
    body: null,
    rawText: error instanceof Error ? error.message : String(error),
    requestKind,
  });
}

/** Throws a ServiceRequestError when the service answers with a failure, and whatever fetch throws when nothing
 *  answers. */
export async function fetchState(): Promise<ScreenStateDocument> {
  const response = await fetch("/api/state");
  if (!response.ok) throw await refusalError(response, "read");
  return (await response.json()) as ScreenStateDocument;
}

/** What a refused answer says about itself: its JSON ``detail``, else its status line. */
export async function describeRefusal(response: Response): Promise<string> {
  const detail = await response.json().then(
    (body: unknown) => (body !== null && typeof body === "object" ? (body as { detail?: unknown }).detail : undefined),
    () => undefined,
  );
  if (typeof detail === "string" && detail) return detail;
  return response.statusText ? `${response.status} ${response.statusText}` : `status ${response.status}`;
}

/** Ask the service to start the stack (the overlay's Start); the failure when it refused or nothing answered. */
export async function postStart(): Promise<Failure | null> {
  try {
    const response = await fetch("/api/start", { method: "POST" });
    if (response.ok) return null;
    const error = await refusalError(response, "change");
    console.warn("[screen] start refused:", error.detail);
    return error.failure;
  } catch (error) {
    console.warn("[screen] start failed:", error);
    return failureOf(error, "change");
  }
}

/** Throws a ServiceRequestError when the service refuses the login (the stack not running, neko refusing). */
export async function postSession(): Promise<NekoSessionAnswer> {
  const response = await fetch("/api/session", { method: "POST" });
  if (!response.ok) throw await refusalError(response, "change");
  return (await response.json()) as NekoSessionAnswer;
}

export interface VerbOutcome {
  ok: boolean;
  detail: string | null;
}

async function postControlVerb(path: string, sessionId: string): Promise<VerbOutcome> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (response.ok) return { ok: true, detail: null };
  return { ok: false, detail: await describeRefusal(response) };
}

export function postTakeControl(sessionId: string): Promise<VerbOutcome> {
  return postControlVerb("/api/control/take", sessionId);
}

export function postReturnControl(sessionId: string): Promise<VerbOutcome> {
  return postControlVerb("/api/control/return", sessionId);
}

/** Drop the page held for the human while an agent drives the browser (the overlay's Cancel), as this session:
 *  the service lets the session in control or the owner drop it. */
export async function postCancelPendingUrl(browserName: string, sessionId: string): Promise<void> {
  const response = await fetch(`/browsers/${encodeURIComponent(browserName)}/pending-url/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!response.ok) console.warn("[screen] cancelling the held page refused:", await describeRefusal(response));
}

/** The controller's stage size (spec section 7.2); the service quantizes and clamps it. */
export async function postScreenSize(
  width: number,
  height: number,
  sessionId: string,
): Promise<ScreenSizeAnswer | null> {
  const response = await fetch("/api/screen-size", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ width, height, session_id: sessionId }),
  });
  if (!response.ok) return null;
  return (await response.json()) as ScreenSizeAnswer;
}

/** This session sent input: the profile chooser's clock (spec section 8.1). */
export async function postInputSeen(sessionId: string): Promise<void> {
  await fetch("/api/input-seen", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
}

export async function fetchConnectInfo(isRelay: boolean): Promise<ConnectInfo> {
  const response = await fetch(isRelay ? "/api/connect-info?relay=1" : "/api/connect-info");
  if (!response.ok) return { ice_servers: [], candidates: [], expires_at: null };
  return (await response.json()) as ConnectInfo;
}

/** Follows ``/api/ws``: every event goes to the handler; the socket is reopened while the page lives. */
export class ServiceEvents {
  private socket: WebSocket | null = null;
  private timer = 0;
  private isClosed = false;

  constructor(private readonly onEvent: (event: ServiceEvent) => void) {}

  start(): void {
    this.open();
  }

  stop(): void {
    this.isClosed = true;
    window.clearTimeout(this.timer);
    this.socket?.close();
  }

  private open(): void {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${window.location.host}/api/ws`);
    this.socket = socket;
    socket.onmessage = (message) => {
      try {
        this.onEvent(JSON.parse(String(message.data)) as ServiceEvent);
      } catch (error) {
        console.warn("[screen] unreadable service event", error);
      }
    };
    socket.onclose = () => {
      if (this.isClosed) return;
      this.timer = window.setTimeout(() => this.open(), RECONNECT_MS);
    };
  }
}
