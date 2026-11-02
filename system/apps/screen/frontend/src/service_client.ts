/** The service's own routes and its ``/api/ws`` event socket, with a reconnecting subscription. */

import type { ConnectInfo, NekoSessionAnswer, ScreenSizeAnswer, ScreenStateDocument, ServiceEvent } from "./state";

const RECONNECT_MS = 1500;

export async function fetchState(): Promise<ScreenStateDocument> {
  const response = await fetch("/api/state");
  if (!response.ok) throw new Error(`state answered ${response.status}`);
  return (await response.json()) as ScreenStateDocument;
}

/** A route of the service refused or failed a request; ``detail`` is what its JSON body said, else the status. */
export class ServiceRequestError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`the Screen service answered ${status}: ${detail}`);
    this.name = "ServiceRequestError";
  }
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

export async function postStart(): Promise<void> {
  const response = await fetch("/api/start", { method: "POST" });
  if (!response.ok) console.warn("[screen] start refused:", await describeRefusal(response));
}

/** Throws a ServiceRequestError when the service refuses the login (the stack not running, neko refusing). */
export async function postSession(): Promise<NekoSessionAnswer> {
  const response = await fetch("/api/session", { method: "POST" });
  if (!response.ok) throw new ServiceRequestError(response.status, await describeRefusal(response));
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
