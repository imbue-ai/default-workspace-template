/**
 * The shell's WebSocket (desktop-interface contracts.md section 6), the one socket this window
 * holds. On connect the server sends ``apps_updated`` and ``desktops_updated``; the window answers
 * with its ``client_state`` (which client it is, on which desktop) and re-sends it on every
 * switch. ``placements_updated``, ``active_desktop_changed``, and the transient ``layout_op`` are
 * how this client's other windows, the shell's own edits, and an agent's ops reach this one.
 */

import { wsUrl } from "@imbue/workspace-ui/src/base-path";
import { ReconnectBackoff } from "@imbue/workspace-ui/src/models/backoff";
import { parseJsonMessage } from "@imbue/workspace-ui/src/models/ws-json";
import { parseAppRecords, parseDesktops } from "../model/records";
import type { AppRecord, Desktop } from "../model/records";

/** The transient ops that reach the browser as messages: the rest are applied to the files. */
export type LayoutOpName = "refresh" | "reload_system_interface";

export interface LayoutOpEvent {
  readonly op: LayoutOpName;
  readonly args: Readonly<Record<string, unknown>>;
  /** ``<app>`` or ``<app>:<marker>`` for the requesting agent, "" when unknown. */
  readonly requester: string;
}

export interface PlacementsUpdatedEvent {
  readonly desktopId: string;
  readonly clientId: string;
  readonly saveId: string;
}

export interface ActiveDesktopChangedEvent {
  readonly clientId: string;
  readonly desktopId: string;
}

export interface SocketHandlers {
  onAppsUpdated(apps: AppRecord[]): void;
  onDesktopsUpdated(desktops: Desktop[]): void;
  onPlacementsUpdated(event: PlacementsUpdatedEvent): void;
  onActiveDesktopChanged(event: ActiveDesktopChangedEvent): void;
  onLayoutOp(event: LayoutOpEvent): void;
  /** The socket (re)opened: the client state is re-reported through ``reportClientState``. */
  onConnected(): void;
}

/** What the store asks of its socket, so a test can stand one in. */
export interface DesktopSocket {
  connect(handlers: SocketHandlers): void;
  /** Report this client's active desktop; ``previousDesktop`` is "" on connect. */
  reportClientState(activeDesktop: string, previousDesktop: string): void;
}

interface RawSocketEvent {
  type?: unknown;
  apps?: unknown;
  desktops?: unknown;
  op?: unknown;
  args?: unknown;
  requester?: unknown;
  target_client_id?: unknown;
  desktop_id?: unknown;
  client_id?: unknown;
  save_id?: unknown;
}

const LAYOUT_OP_NAMES: readonly string[] = ["refresh", "reload_system_interface"];

export class ShellSocket implements DesktopSocket {
  private ws: WebSocket | null = null;
  private handlers: SocketHandlers | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly reconnectBackoff = new ReconnectBackoff();

  constructor(private readonly clientId: string) {}

  connect(handlers: SocketHandlers): void {
    this.handlers = handlers;
    this.open();
  }

  reportClientState(activeDesktop: string, previousDesktop: string): void {
    if (this.ws === null || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(
      JSON.stringify({
        type: "client_state",
        client_id: this.clientId,
        active_desktop: activeDesktop,
        previous_desktop: previousDesktop,
      }),
    );
  }

  private open(): void {
    if (this.ws !== null) return;
    const url = wsUrl("/api/ws");
    console.info(`[si-ws] connecting to ${url}`);
    const ws = new WebSocket(url);
    this.ws = ws;
    ws.onopen = () => {
      console.info("[si-ws] connected");
      this.reconnectBackoff.reset();
      this.handlers?.onConnected();
    };
    ws.onmessage = (event: MessageEvent) => {
      const data = parseJsonMessage<RawSocketEvent>(event.data as string);
      if (data !== null) this.handleEvent(data);
    };
    ws.onclose = (event: CloseEvent) => {
      console.warn(
        `[si-ws] closed (code=${event.code} reason=${JSON.stringify(event.reason)} wasClean=${event.wasClean})`,
      );
      this.ws = null;
      this.scheduleReconnect();
    };
    ws.onerror = () => {
      console.warn("[si-ws] socket error");
      ws.close();
    };
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    const delayMs = this.reconnectBackoff.nextDelay();
    console.info(`[si-ws] reconnecting in ${delayMs}ms`);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, delayMs);
  }

  private handleEvent(event: RawSocketEvent): void {
    const handlers = this.handlers;
    if (handlers === null) return;
    switch (event.type) {
      case "apps_updated":
        handlers.onAppsUpdated(parseAppRecords(event.apps));
        return;
      case "desktops_updated":
        handlers.onDesktopsUpdated(parseDesktops(event.desktops));
        return;
      case "placements_updated":
        handlers.onPlacementsUpdated({
          desktopId: String(event.desktop_id ?? ""),
          clientId: String(event.client_id ?? ""),
          saveId: String(event.save_id ?? ""),
        });
        return;
      case "active_desktop_changed":
        handlers.onActiveDesktopChanged({
          clientId: String(event.client_id ?? ""),
          desktopId: String(event.desktop_id ?? ""),
        });
        return;
      case "layout_op": {
        // A targeted op is for one client's windows; an untargeted one (a refresh of a whole app,
        // the interface reload) is for every window.
        if (event.target_client_id != null && event.target_client_id !== this.clientId) return;
        if (typeof event.op !== "string" || !LAYOUT_OP_NAMES.includes(event.op)) return;
        const args =
          event.args !== null && typeof event.args === "object" ? (event.args as Record<string, unknown>) : {};
        handlers.onLayoutOp({
          op: event.op as LayoutOpName,
          args,
          requester: typeof event.requester === "string" ? event.requester : "",
        });
        return;
      }
      default:
        return;
    }
  }
}
