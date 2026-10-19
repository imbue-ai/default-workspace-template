/** The media path: neko's offer arrives over its socket (``signal/provide``), the answer and candidates go back
 *  the same way, the H.264 track lands in the ``<video>`` (docs/system/specs/screen/spec.md section 5.3). Server
 *  candidates are rewritten to the endpoints ``connect-info`` named; the peer connection carries its ICE servers
 *  and ``iceTransportPolicy: "all"``, so a direct pair wins by priority whenever one connects. The ``IcePolicy``
 *  escalates a connection that does not come up: relay servers and an ICE restart, then the screencast. */

import { candidatesToAdd } from "./candidates";
import { IcePolicy } from "./ice_policy";
import type { NekoSocket } from "./neko_socket";
import { pageStorage, rememberNeedsRelay } from "./relay_memory";
import { windowScheduler } from "./scheduler";
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
  private isAudioEnabled = false;
  private isReleased = false;
  private connectInfo: ConnectInfo = EMPTY_CONNECT_INFO;
  private isRelayAttempt = false;
  private status: StreamStatus = "idle";
  readonly policy: IcePolicy;
  private readonly onStatus: (status: StreamStatus) => void;

  constructor(
    private readonly socket: NekoSocket,
    private readonly video: HTMLVideoElement,
    onStatusChanged: (status: StreamStatus) => void,
    /** The data channel neko opens on every peer (the cursor comes over it, ``remote_cursor.ts``). */
    private readonly onDataChannel: (channel: RTCDataChannel) => void = () => undefined,
  ) {
    this.onStatus = (status) => {
      this.status = status;
      onStatusChanged(status);
    };
    this.policy = new IcePolicy(
      {
        fetchRelayInfo: () => fetchConnectInfo(true),
        restartIce: (info) => this.restartIce(info, true),
        fallbackToScreencast: () => this.fallbackToScreencast(),
        retryFromScreencast: (info) => this.restartIce(info, false),
        rememberRelay: () => rememberNeedsRelay(pageStorage()),
      },
      windowScheduler,
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
      this.pc
        ?.setRemoteDescription({ type: "answer", sdp: String(payload.sdp ?? "") })
        .catch((error) => console.warn("[screen] neko's answer was not accepted", error));
    });
    socket.on("signal/candidate", (payload) => {
      const candidate = payload as unknown as RTCIceCandidateInit;
      if (this.pc?.remoteDescription) this.addCandidate(candidate);
      else this.pending.push(candidate);
    });
    socket.on("signal/close", () => {
      // neko echoes a close the viewer asked for; an unasked one means the server dropped its peer, and a fresh
      // one is asked for with the same endpoints, no escalation.
      if (this.pc === null || this.isReleased) return;
      this.pc.close();
      this.pc = null;
      if (this.status !== "screencast") this.onStatus("connecting");
      window.clearTimeout(this.retryTimer);
      this.retryTimer = window.setTimeout(
        () => this.request(this.videoId, this.connectInfo, this.isRelayAttempt),
        REREQUEST_MS,
      );
    });
  }

  /** Ask neko for a peer with the announced video profile; audio stays off until unmuted (spec section 8.3).
   *  ``isRelay`` says whether ``connectInfo`` was fetched with ``relay=1``. */
  request(videoId: string, connectInfo: ConnectInfo, isRelay: boolean): void {
    this.videoId = videoId;
    this.connectInfo = connectInfo;
    this.isRelayAttempt = isRelay;
    this.pending = [];
    this.isReleased = false;
    // A re-request while the screencast shows the Screen (the server dropped a retry's peer) keeps it showing.
    if (this.status !== "screencast") this.onStatus("connecting");
    this.socket.send("signal/request", {
      video: { selector: { id: videoId, type: "exact" } },
      audio: { disabled: !this.isAudioEnabled },
    });
    this.policy.attemptStarted(isRelay);
  }

  /** Whether a peer is up or being set up. */
  get hasPeer(): boolean {
    return this.pc !== null;
  }

  /** What ``connect-info`` last told this stream, and whether the attempt in flight carries relay servers. */
  get lastConnectInfo(): ConnectInfo {
    return this.connectInfo;
  }

  get isRelay(): boolean {
    return this.isRelayAttempt;
  }

  /** The peer's statistics, or null without a peer. */
  getStats(): Promise<RTCStatsReport> | null {
    return this.pc === null ? null : this.pc.getStats();
  }

  /** The user asked for another WebRTC try from the screencast. */
  retryNow(): void {
    this.policy.retryNow();
  }

  selectVideo(videoId: string): void {
    this.videoId = videoId;
    if (this.pc) this.socket.send("signal/video", { selector: { id: videoId, type: "exact" } });
  }

  /** Audio for this session alone: neko unpauses the track for an enabled session (spec section 8.3). */
  setAudioEnabled(isEnabled: boolean): void {
    this.isAudioEnabled = isEnabled;
    this.video.muted = !isEnabled;
    if (this.pc) this.socket.send("signal/audio", { disabled: !isEnabled });
  }

  /** Close the media on purpose (the viewer is hidden, spec section 8.2): neko drops this peer and, with the last
   *  one, the encoder; the socket and the session stay. */
  release(): void {
    this.isReleased = true;
    window.clearTimeout(this.retryTimer);
    this.policy.reset();
    if (this.pc) {
      this.socket.send("signal/close");
      this.pc.close();
      this.pc = null;
    }
    this.video.srcObject = null;
    this.onStatus("idle");
  }

  close(): void {
    window.clearTimeout(this.retryTimer);
    this.isReleased = true;
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
   *  an ICE-restart offer); a peer that cannot take them, or none, gets a fresh offer instead. A retry from the
   *  screencast (``isStatusShown`` false) leaves the status alone, so the screencast keeps showing the Screen until
   *  the new peer connects. */
  private restartIce(info: ConnectInfo | null, isStatusShown: boolean): void {
    if (info !== null) this.connectInfo = info;
    this.isRelayAttempt = true;
    this.pending = [];
    if (this.pc !== null && this.pc.connectionState !== "closed") {
      try {
        this.pc.setConfiguration(this.configuration());
        this.socket.send("signal/restart");
        if (isStatusShown) this.onStatus("connecting");
        return;
      } catch (error) {
        console.warn("[screen] the peer could not take new ICE servers; asking for a fresh one", error);
      }
    }
    this.pc?.close();
    this.pc = null;
    if (isStatusShown) this.onStatus("connecting");
    this.socket.send("signal/request", {
      video: { selector: { id: this.videoId, type: "exact" } },
      audio: { disabled: !this.isAudioEnabled },
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
      this.pc
        ?.addIceCandidate(rewritten)
        .catch((error) => console.warn("[screen] a server candidate was not accepted", rewritten.candidate, error));
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
    pc.ondatachannel = (event) => this.onDataChannel(event.channel);
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
    const isScreencastRetry = this.policy.phase === "relay" && this.status === "screencast";
    if (state === "connected") {
      this.policy.connected();
      this.onStatus("connected");
    } else if (state === "disconnected") {
      if (!isScreencastRetry) this.onStatus("connecting");
    } else if (state === "failed") {
      if (!isScreencastRetry) this.onStatus("connecting");
      this.policy.failed();
    }
  }
}
