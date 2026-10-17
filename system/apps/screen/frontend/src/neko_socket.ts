/** neko's WebSocket, reached through the service's ``/neko/`` proxy with the session token: ``{event, payload}``
 *  messages both ways (docs/system/specs/screen/spec.md section 2.3). */

export type NekoEventHandler = (payload: Record<string, unknown>) => void;

const RECONNECT_MS = 1500;

export class NekoSocket {
  private socket: WebSocket | null = null;
  private readonly handlers = new Map<string, NekoEventHandler[]>();
  private timer = 0;
  private isClosed = false;

  constructor(
    private readonly token: string,
    private readonly onOpen: () => void,
  ) {}

  on(event: string, handler: NekoEventHandler): void {
    const list = this.handlers.get(event) ?? [];
    list.push(handler);
    this.handlers.set(event, list);
  }

  send(event: string, payload?: unknown): void {
    if (this.socket === null || this.socket.readyState !== WebSocket.OPEN) return;
    this.socket.send(JSON.stringify(payload === undefined ? { event } : { event, payload }));
  }

  get isOpen(): boolean {
    return this.socket !== null && this.socket.readyState === WebSocket.OPEN;
  }

  start(): void {
    this.open();
  }

  stop(): void {
    this.isClosed = true;
    window.clearTimeout(this.timer);
    this.socket?.close();
    this.socket = null;
  }

  private open(): void {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(
      `${protocol}//${window.location.host}/neko/api/ws?token=${encodeURIComponent(this.token)}`,
    );
    this.socket = socket;
    socket.onopen = () => this.onOpen();
    socket.onmessage = (message) => {
      let parsed: { event?: unknown; payload?: unknown };
      try {
        parsed = JSON.parse(String(message.data)) as { event?: unknown; payload?: unknown };
      } catch (error) {
        console.warn("[screen] unreadable neko message", error);
        return;
      }
      if (typeof parsed.event !== "string") return;
      if (parsed.event === "system/heartbeat") this.send("client/heartbeat");
      const payload = (parsed.payload ?? {}) as Record<string, unknown>;
      for (const handler of this.handlers.get(parsed.event) ?? []) handler(payload);
    };
    socket.onclose = () => {
      if (this.isClosed) return;
      this.timer = window.setTimeout(() => this.open(), RECONNECT_MS);
    };
  }
}
