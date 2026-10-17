/** The media path: neko's offer arrives over its socket (``signal/provide``), the answer and candidates go back
 *  the same way, the H.264 track lands in the ``<video>`` (docs/system/specs/screen/spec.md section 5.3). Server
 *  candidates are rewritten to the endpoints ``connect-info`` named; the peer connection carries its ICE servers
 *  and ``iceTransportPolicy: "all"``, so a direct pair wins by priority whenever one connects. The ``IcePolicy``
 *  escalates a connection that does not come up: relay servers and an ICE restart, then the screencast. */

import { candidatesToAdd } from "./candidates";
import { IcePolicy } from "./ice_policy";
import type { NekoSocket } from "./neko_socket";
import { pageStorage, rememberNeedsRelay } from "./relay_memory";
import { fetchConnectInfo } from "./service_client";
import type { ConnectInfo } from "./state";
import { EMPTY_CONNECT_INFO } from "./state";

export type StreamStatus = "idle" | "connecting" | "connected" | "failed" | "screencast";

// How long after the server closed its peer a new one is asked for.
const REREQUEST_MS = 1500;

export class NekoStream {
  private pc: RTCPeerConnection | null = null;
  private pending: RTCIceCandidateInit[] = [];
  private retryTimer = 0;
  private videoId = "watch";
  private connectInfo: ConnectInfo = EMPTY_CONNECT_INFO;
  private isRelayAttempt = false;
  readonly policy: IcePolicy;

  constructor(
    private readonly socket: NekoSocket,
    private readonly video: HTMLVideoElement,
    private readonly onStatus: (status: StreamStatus) => void,
  ) {
    this.policy = new IcePolicy(
      {
        fetchRelayInfo: () => fetchConnectInfo(true),
        restartIce: (info) => this.restartIce(info),
        fallbackToScreencast: () => this.fallbackToScreencast(),
        rememberRelay: () => rememberNeedsRelay(pageStorage()),
      },
      { setTimeout: (callback, ms) => window.setTimeout(callback, ms), clearTimeout: (id) => window.clearTimeout(id) },
    );
    socket.on("signal/provide", (payload) => {
      this.onProvide(payload).catch((error) => console.error("[screen] webrtc setup failed", error));
    });
    const renegotiate = (payload: Record<string, unknown>) => {
      this.onOffer(String(payload.sdp ?? "")).catch((error) => console.error("[screen] renegotiation failed", error));
    };
    socket.on("signal/offer", renegotiate);
    socket.on("signal/restart", renegotiate);
    socket.on("signal/answer", (payload) => {
      this.pc?.setRemoteDescription({ type: "answer", sdp: String(payload.sdp ?? "") }).catch(() => undefined);
    });
    socket.on("signal/candidate", (payload) => {
      const candidate = payload as unknown as RTCIceCandidateInit;
      if (this.pc?.remoteDescription) this.addCandidate(candidate);
      else this.pending.push(candidate);
    });
    socket.on("signal/close", () => {
      // The server dropped its peer; a fresh one is asked for with the same endpoints, no escalation.
      if (this.pc === null) return;
      this.pc.close();
      this.pc = null;
      this.onStatus("connecting");
      window.clearTimeout(this.retryTimer);
      this.retryTimer = window.setTimeout(
        () => this.request(this.videoId, this.connectInfo, this.isRelayAttempt),
        REREQUEST_MS,
      );
    });
  }

  /** Ask neko for a peer with the announced video profile; audio stays off (spec section 8.3). ``isRelay`` says
   *  whether ``connectInfo`` was fetched with ``relay=1``. */
  request(videoId: string, connectInfo: ConnectInfo, isRelay: boolean): void {
    this.videoId = videoId;
    this.connectInfo = connectInfo;
    this.isRelayAttempt = isRelay;
    this.pending = [];
    this.onStatus("connecting");
    this.socket.send("signal/request", {
      video: { selector: { id: videoId, type: "exact" } },
      audio: { disabled: true },
    });
    this.policy.attemptStarted(isRelay);
  }

  selectVideo(videoId: string): void {
    this.videoId = videoId;
    if (this.pc) this.socket.send("signal/video", { selector: { id: videoId, type: "exact" } });
  }

  close(): void {
    window.clearTimeout(this.retryTimer);
    this.policy.reset();
    this.pc?.close();
    this.pc = null;
    this.video.srcObject = null;
    this.onStatus("idle");
  }

  private configuration(): RTCConfiguration {
    return { iceServers: this.connectInfo.ice_servers, iceTransportPolicy: "all" };
  }

  /** Relay servers arrived: give them to the live peer and have neko restart ICE (``signal/restart`` answers with
   *  an ICE-restart offer); a peer that cannot take them, or none, gets a fresh offer instead. */
  private restartIce(info: ConnectInfo | null): void {
    if (info !== null) this.connectInfo = info;
    this.isRelayAttempt = true;
    this.pending = [];
    if (this.pc !== null && this.pc.connectionState !== "closed") {
      try {
        this.pc.setConfiguration(this.configuration());
        this.socket.send("signal/restart");
        this.onStatus("connecting");
        return;
      } catch (error) {
        console.warn("[screen] the peer could not take new ICE servers; asking for a fresh one", error);
      }
    }
    this.pc?.close();
    this.pc = null;
    this.onStatus("connecting");
    this.socket.send("signal/request", {
      video: { selector: { id: this.videoId, type: "exact" } },
      audio: { disabled: true },
    });
  }

  private fallbackToScreencast(): void {
    this.pc?.close();
    this.pc = null;
    this.video.srcObject = null;
    this.onStatus("screencast");
  }

  private addCandidate(candidate: RTCIceCandidateInit): void {
    for (const rewritten of candidatesToAdd(candidate, this.connectInfo)) {
      this.pc?.addIceCandidate(rewritten).catch(() => undefined);
    }
  }

  private async onProvide(payload: Record<string, unknown>): Promise<void> {
    window.clearTimeout(this.retryTimer);
    this.pc?.close();
    const pc = new RTCPeerConnection(this.configuration());
    this.pc = pc;
    pc.onicecandidate = (event) => {
      if (event.candidate) this.socket.send("signal/candidate", event.candidate.toJSON());
    };
    pc.ontrack = (event) => {
      if (event.track.kind !== "video") return;
      if ("jitterBufferTarget" in event.receiver)
        (event.receiver as { jitterBufferTarget: number }).jitterBufferTarget = 0;
      this.video.srcObject = event.streams[0];
      this.video.play().catch(() => undefined);
    };
    pc.onconnectionstatechange = () => {
      if (this.pc === pc) this.onPeerState(pc.connectionState);
    };
    await this.onOffer(String(payload.sdp ?? ""));
  }

  private async onOffer(sdp: string): Promise<void> {
    const pc = this.pc;
    if (!pc || !sdp) return;
    await pc.setRemoteDescription({ type: "offer", sdp });
    for (const candidate of this.pending.splice(0)) this.addCandidate(candidate);
    await pc.setLocalDescription(await pc.createAnswer());
    this.socket.send("signal/answer", { sdp: pc.localDescription?.sdp ?? "" });
  }

  private onPeerState(state: RTCPeerConnectionState): void {
    if (state === "connected") {
      this.policy.connected();
      this.onStatus("connected");
    } else if (state === "disconnected") {
      this.onStatus("connecting");
    } else if (state === "failed") {
      this.onStatus("connecting");
      this.policy.failed();
    }
  }
}
