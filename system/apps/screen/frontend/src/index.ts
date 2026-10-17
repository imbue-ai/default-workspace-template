/**
 * The Screen's viewer (docs/system/specs/screen/spec.md section 11): the live desktop in a video element, the
 * strip naming who has control, the overlays for a stack that is installing, starting or stopped, and input sent
 * to neko while this viewer's session is the controller. It connects to the shell that frames it, reporting ``/``
 * with the title the service tracks, and owns the close chord (Ctrl+W goes to the remote).
 */

import "./style.css";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";
import { InputRelay } from "./input";
import { NekoSocket } from "./neko_socket";
import { Overlays } from "./overlays";
import {
  ServiceEvents,
  fetchConnectInfo,
  fetchState,
  postReturnControl,
  postSession,
  postStart,
  postTakeControl,
} from "./service_client";
import type { NekoSessionAnswer, ScreenStateDocument, ServiceEvent } from "./state";
import { isOwnSessionController } from "./state";
import { Strip } from "./strip";
import { NekoStream, type StreamStatus } from "./webrtc";

export const PAGE_PATH = "/";

class Viewer {
  private readonly root = document.createElement("div");
  private readonly stage = document.createElement("div");
  private readonly video = document.createElement("video");
  private readonly strip: Strip;
  private readonly overlays: Overlays;
  private state: ScreenStateDocument | null = null;
  private session: NekoSessionAnswer | null = null;
  private socket: NekoSocket | null = null;
  private stream: NekoStream | null = null;
  private input: InputRelay | null = null;
  private streamStatus: StreamStatus = "idle";
  private isSigningIn = false;
  private reportLocation: ((title: string) => void) | null = null;

  constructor(mount: HTMLElement) {
    this.root.className = "screen-root";
    this.strip = new Strip({
      onTakeControl: () => this.takeControl(),
      onReturnControl: () => this.returnControl(),
    });
    this.overlays = new Overlays(
      () => void postStart(),
      () => this.takeControl(),
    );
    this.stage.className = "screen-stage screen-stage--not-controlling";
    this.stage.tabIndex = 0;
    this.stage.dataset.screenStage = "true";
    this.video.autoplay = true;
    this.video.playsInline = true;
    this.video.muted = true;
    this.stage.append(this.video, this.overlays.dimElement, this.overlays.element);
    this.root.append(this.strip.element, this.stage);
    mount.append(this.root);
    window.addEventListener("pagehide", () => this.logout());
  }

  attachShell(reportLocation: (title: string) => void): void {
    this.reportLocation = reportLocation;
    if (this.state) reportLocation(this.state.title);
  }

  sendCloseChord(): void {
    this.input?.sendCloseChord();
  }

  handleEvent(event: ServiceEvent): void {
    if (event.type === "state") {
      const { type: _type, ...document } = event;
      this.applyState(document);
    } else if (event.type === "stopped") {
      this.teardownStream();
    } else if (event.type === "handoff_request") {
      // The strip already shows the queue; the overlay's dim lifts as the lease moved to nobody.
    }
  }

  private applyState(state: ScreenStateDocument): void {
    const previousTitle = this.state?.title;
    this.state = state;
    if (state.title !== previousTitle) this.reportLocation?.(state.title);
    if (state.lifecycle === "running") {
      if (this.session === null) void this.signIn();
      else if (this.stream && this.state.video_id) this.stream.selectVideo(state.video_id);
    } else if (this.session !== null) {
      this.teardownStream();
    }
    if (this.input && state.screen_size) this.input.screen = state.screen_size;
    this.render();
  }

  private render(): void {
    if (!this.state) return;
    const ownSessionId = this.session?.session_id ?? null;
    const isController = isOwnSessionController(this.state.control, ownSessionId);
    // Input flows while this session controls, and while nobody does: the first input is then neko's
    // control/request, which it grants with no host, and the service records this human (spec section 6.2).
    const canSend = (isController || this.state.control.controller === "nobody") && this.state.lifecycle === "running";
    this.strip.update(this.state.control, ownSessionId, this.streamStatus);
    this.overlays.update(this.state, this.streamStatus, this.session !== null);
    this.input?.setEnabled(canSend);
    this.stage.classList.toggle("screen-stage--not-controlling", !canSend);
    this.stage.dataset.screenController = isController ? "you" : this.state.control.controller;
  }

  private async signIn(): Promise<void> {
    if (this.isSigningIn) return;
    this.isSigningIn = true;
    try {
      const session = await postSession();
      if (session === null) return;
      this.session = session;
      const socket = new NekoSocket(session.token, () => this.onNekoOpen());
      this.socket = socket;
      this.stream = new NekoStream(socket, this.video, (status) => {
        this.streamStatus = status;
        this.render();
      });
      this.input = new InputRelay(socket, this.stage);
      if (this.state?.screen_size) this.input.screen = this.state.screen_size;
      socket.on("screen/updated", (payload) => {
        if (this.input) this.input.screen = { width: Number(payload.width), height: Number(payload.height) };
      });
      socket.on("system/init", (payload) => {
        const size = payload.screen_size as { width?: number; height?: number } | undefined;
        if (this.input && size?.width && size?.height) this.input.screen = { width: size.width, height: size.height };
      });
      socket.start();
      this.render();
    } finally {
      this.isSigningIn = false;
    }
  }

  private onNekoOpen(): void {
    if (!this.stream || !this.state) return;
    const videoId = this.state.video_id;
    fetchConnectInfo(false)
      .then((connectInfo) => this.stream?.request(videoId, connectInfo))
      .catch((error) => console.error("[screen] connect-info failed", error));
  }

  private teardownStream(): void {
    this.stream?.close();
    this.socket?.stop();
    this.stream = null;
    this.socket = null;
    this.input = null;
    this.session = null;
    this.streamStatus = "idle";
    this.render();
  }

  private logout(): void {
    if (this.session === null) return;
    const token = this.session.token;
    this.session = null;
    // The page is going away: a keepalive request outlives it (spec section 6.3).
    void fetch("/neko/api/logout", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      keepalive: true,
    });
  }

  private async takeControl(): Promise<void> {
    if (this.session === null) return;
    await postTakeControl(this.session.session_id);
    this.stage.focus();
  }

  private async returnControl(): Promise<void> {
    if (this.session === null) return;
    await postReturnControl(this.session.session_id);
  }
}

function bootstrap(): void {
  const mount = document.getElementById("app");
  if (mount === null) return;
  const viewer = new Viewer(mount);
  let handshake: ShellHandshake | null = null;
  const connection = connectToShell({
    capabilities: { navigation: false, closeChord: true },
    onHandshake: (received) => {
      handshake = received;
      viewer.attachShell((title) => connection.location(PAGE_PATH, title));
    },
    onCloseRequest: () => viewer.sendCloseChord(),
  });
  window.addEventListener("focus", () => connection.focused());
  installElementContextMenu({ connection, handshake: () => handshake, open: createContextMenuOpener().open });
  const events = new ServiceEvents((event) => viewer.handleEvent(event));
  fetchState()
    .then((state) => viewer.handleEvent({ type: "state", ...state }))
    .catch((error) => console.error("[screen] state failed", error))
    .finally(() => events.start());
}

window.addEventListener("load", bootstrap);
