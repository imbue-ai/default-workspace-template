/** The service's own routes and its ``/api/ws`` event socket, with a reconnecting subscription. */

import type { ConnectInfo, NekoSessionAnswer, ScreenStateDocument, ServiceEvent } from "./state";

const RECONNECT_MS = 1500;

export async function fetchState(): Promise<ScreenStateDocument> {
  const response = await fetch("/api/state");
  if (!response.ok) throw new Error(`state answered ${response.status}`);
  return (await response.json()) as ScreenStateDocument;
}

export async function postStart(): Promise<void> {
  await fetch("/api/start", { method: "POST" });
}

export async function postSession(): Promise<NekoSessionAnswer | null> {
  const response = await fetch("/api/session", { method: "POST" });
  if (!response.ok) return null;
  return (await response.json()) as NekoSessionAnswer;
}

export async function postTakeControl(sessionId: string): Promise<boolean> {
  const response = await fetch("/api/control/take", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  return response.ok;
}

export async function postReturnControl(sessionId: string): Promise<boolean> {
  const response = await fetch("/api/control/return", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  return response.ok;
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
