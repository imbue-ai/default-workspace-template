/** The stream's status over a fake neko socket and a fake peer connection: while the screencast shows the Screen, a
 *  peer that fails or is set up again leaves it showing (spec section 5.4). */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { NekoEventHandler, NekoSocket } from "./neko_socket";
import type { ConnectInfo } from "./state";
import { EMPTY_CONNECT_INFO } from "./state";
import { NekoStream } from "./webrtc";
import type { StreamStatus } from "./webrtc";

const RELAY_INFO: ConnectInfo = {
  ice_servers: [{ urls: "turn:relay.example:3478", username: "u", credential: "c" }],
  candidates: [],
  expires_at: null,
};

class FakeSocket {
  readonly sent: string[] = [];
  private readonly handlers = new Map<string, NekoEventHandler[]>();

  on(event: string, handler: NekoEventHandler): void {
    this.handlers.set(event, [...(this.handlers.get(event) ?? []), handler]);
  }

  send(event: string): void {
    this.sent.push(event);
  }

  emit(event: string, payload: Record<string, unknown> = {}): void {
    for (const handler of this.handlers.get(event) ?? []) handler(payload);
  }
}

/** What ``NekoStream`` touches on a peer; the state changes are the test's to make. */
class FakePeerConnection {
  static instances: FakePeerConnection[] = [];
  connectionState: RTCPeerConnectionState = "new";
  remoteDescription: RTCSessionDescriptionInit | null = null;
  localDescription: RTCSessionDescriptionInit | null = null;
  onicecandidate: (() => void) | null = null;
  ontrack: (() => void) | null = null;
  onconnectionstatechange: (() => void) | null = null;
  ondatachannel: (() => void) | null = null;

  constructor() {
    FakePeerConnection.instances.push(this);
  }

  async setRemoteDescription(description: RTCSessionDescriptionInit): Promise<void> {
    this.remoteDescription = description;
  }

  async createAnswer(): Promise<RTCSessionDescriptionInit> {
    return { type: "answer", sdp: "v=0 answer" };
  }

  async setLocalDescription(description: RTCSessionDescriptionInit): Promise<void> {
    this.localDescription = description;
  }

  async addIceCandidate(): Promise<void> {}

  setConfiguration(): void {}

  close(): void {
    this.connectionState = "closed";
  }

  /** The peer's state changed; the stream hears of it as a browser would tell it. */
  becomes(state: RTCPeerConnectionState): void {
    this.connectionState = state;
    this.onconnectionstatechange?.();
  }
}

/** Lets the stream's answer to a provide (its awaits on the fake peer) run to the end. */
async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("NekoStream", () => {
  let socket: FakeSocket;
  let statuses: StreamStatus[];
  let stream: NekoStream;

  beforeEach(() => {
    FakePeerConnection.instances = [];
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("fetch", async () => ({ ok: true, json: async () => RELAY_INFO }));
    socket = new FakeSocket();
    statuses = [];
    stream = new NekoStream(socket as unknown as NekoSocket, document.createElement("video"), (status) =>
      statuses.push(status),
    );
  });

  afterEach(() => {
    stream.close();
    vi.unstubAllGlobals();
  });

  async function provide(): Promise<FakePeerConnection> {
    socket.emit("signal/provide", { sdp: "v=0 offer" });
    await settle();
    return FakePeerConnection.instances[FakePeerConnection.instances.length - 1];
  }

  /** A relay attempt whose peer failed: the policy falls back to the screencast. */
  async function fallBack(): Promise<void> {
    stream.request("watch", EMPTY_CONNECT_INFO, true);
    const peer = await provide();
    peer.becomes("failed");
    expect(statuses).toEqual(["connecting", "connecting", "screencast"]);
    expect(stream.policy.phase).toBe("screencast");
    statuses.length = 0;
  }

  it("says connecting again while a peer fails before the screencast", async () => {
    stream.request("watch", EMPTY_CONNECT_INFO, false);
    const peer = await provide();
    peer.becomes("disconnected");
    expect(statuses).toEqual(["connecting", "connecting"]);
    expect(socket.sent).toEqual(["signal/request", "signal/answer"]);
  });

  it("keeps the screencast showing while a retry's peer fails, and ends it when one connects", async () => {
    await fallBack();
    stream.retryNow();
    await settle();
    expect(stream.policy.phase).toBe("relay");
    expect(statuses).toEqual([]);
    const retried = await provide();
    retried.becomes("disconnected");
    retried.becomes("failed");
    expect(statuses).toEqual(["screencast"]);
    expect(stream.policy.phase).toBe("screencast");
    stream.retryNow();
    await settle();
    const connected = await provide();
    connected.becomes("connected");
    expect(statuses).toEqual(["screencast", "connected"]);
  });

  it("keeps the screencast showing while a late peer fails", async () => {
    // A provide the stream had asked for before falling back arrives afterwards; the peer it builds fails.
    await fallBack();
    const late = await provide();
    late.becomes("failed");
    expect(statuses).toEqual([]);
    expect(stream.policy.phase).toBe("screencast");
  });
});
