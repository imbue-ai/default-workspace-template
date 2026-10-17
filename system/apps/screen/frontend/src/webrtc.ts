/** The media path: neko's offer arrives over its socket (``signal/provide``), the answer and candidates go back
 *  the same way, the H.264 track lands in the ``<video>`` (docs/system/specs/screen/spec.md section 5.3). */

import { candidatesToAdd } from "./candidates";
import type { NekoSocket } from "./neko_socket";
import type { ConnectInfo } from "./state";

export type StreamStatus = "idle" | "connecting" | "connected" | "failed";

const RECONNECT_BACKOFF_MS = 1500;
const RECONNECT_MAX = 10;

export class NekoStream {
  private pc: RTCPeerConnection | null = null;
  private pending: RTCIceCandidateInit[] = [];
  private failures = 0;
  private retryTimer = 0;
  private videoId = "watch";
  private isAudioEnabled = false;
  private isReleased = false;
  private connectInfo: ConnectInfo = { ice_servers: [], candidates: [], expires_at: null };

  constructor(
    private readonly socket: NekoSocket,
    private readonly video: HTMLVideoElement,
    private readonly onStatus: (status: StreamStatus) => void,
  ) {
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
      // neko echoes a close the viewer asked for; only an unasked one is a failure to recover from.
      if (this.pc && !this.isReleased) this.onPeerState("failed");
    });
  }

  /** Ask neko for a peer with the announced video profile; audio stays off until unmuted (spec section 8.3). */
  request(videoId: string, connectInfo: ConnectInfo): void {
    this.videoId = videoId;
    this.connectInfo = connectInfo;
    this.pending = [];
    this.isReleased = false;
    this.onStatus("connecting");
    this.socket.send("signal/request", {
      video: { selector: { id: videoId, type: "exact" } },
      audio: { disabled: !this.isAudioEnabled },
    });
  }

  /** Whether a peer is up or being set up. */
  get hasPeer(): boolean {
    return this.pc !== null;
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
    if (!this.pc) return;
    this.isReleased = true;
    window.clearTimeout(this.retryTimer);
    this.socket.send("signal/close");
    this.pc.close();
    this.pc = null;
    this.video.srcObject = null;
    this.onStatus("idle");
  }

  close(): void {
    window.clearTimeout(this.retryTimer);
    this.isReleased = true;
    this.pc?.close();
    this.pc = null;
    this.video.srcObject = null;
    this.onStatus("idle");
  }

  private addCandidate(candidate: RTCIceCandidateInit): void {
    for (const rewritten of candidatesToAdd(candidate, this.connectInfo)) {
      this.pc?.addIceCandidate(rewritten).catch(() => undefined);
    }
  }

  private async onProvide(payload: Record<string, unknown>): Promise<void> {
    window.clearTimeout(this.retryTimer);
    this.pc?.close();
    const pc = new RTCPeerConnection({ iceServers: this.connectInfo.ice_servers, iceTransportPolicy: "all" });
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
      this.failures = 0;
      this.onStatus("connected");
    } else if (state === "disconnected") {
      this.onStatus("connecting");
    } else if (state === "failed") {
      this.pc?.close();
      this.pc = null;
      if (++this.failures > RECONNECT_MAX) {
        this.onStatus("failed");
        return;
      }
      this.onStatus("connecting");
      window.clearTimeout(this.retryTimer);
      this.retryTimer = window.setTimeout(
        () => this.request(this.videoId, this.connectInfo),
        RECONNECT_BACKOFF_MS * Math.min(this.failures, 4),
      );
    }
  }
}
