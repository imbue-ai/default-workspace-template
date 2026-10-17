/**
 * The Screen's viewer (docs/system/specs/screen/spec.md section 11): the live desktop in a video element, the
 * strip naming who has control, the overlays for a stack that is installing, starting or stopped, and input sent
 * to neko while this viewer's session is the controller and the viewer is visible. It connects to the shell that
 * frames it, reporting ``/`` with the title the service tracks, and owns the close chord (Ctrl+W goes to the
 * remote). The controller's stage size drives the Screen's size (section 7.2), the service's profile drives the
 * video every viewer selects (section 8.1), a hidden viewer releases its media (section 8.2), and audio is off
 * until unmuted (section 8.3).
 */

import "./style.css";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";
import { PHONE_MEDIA_QUERY } from "@imbue/workspace-ui/src/device_queries";
import { InputRelay } from "./input";
import { NekoSocket } from "./neko_socket";
import { Overlays } from "./overlays";
import { ResizeReporter } from "./resize_reporter";
import { windowScheduler } from "./scheduler";
import {
  ServiceEvents,
  fetchConnectInfo,
  fetchState,
  postInputSeen,
  postReturnControl,
  postScreenSize,
  postSession,
  postStart,
  postTakeControl,
} from "./service_client";
import type { NekoSessionAnswer, ScreenStateDocument, ServiceEvent } from "./state";
import { isOwnSessionController } from "./state";
import { Strip } from "./strip";
import { REEVALUATE_INTERVAL_MS, ViewerVisibility } from "./visibility";
import { NekoStream, type StreamStatus } from "./webrtc";

export const PAGE_PATH = "/";

/** How often at most the input relay tells the service this session is active (its window is ten seconds). */
const INPUT_SEEN_THROTTLE_MS = 2000;

class Viewer {
  private readonly root = document.createElement("div");
  private readonly stage = document.createElement("div");
  private readonly video = document.createElement("video");
  private readonly strip: Strip;
  private readonly overlays: Overlays;
  private readonly visibility: ViewerVisibility;
  private readonly resizeReporter: ResizeReporter;
  private readonly isPhone: boolean;
  private state: ScreenStateDocument | null = null;
  private session: NekoSessionAnswer | null = null;
  private socket: NekoSocket | null = null;
  private stream: NekoStream | null = null;
  private input: InputRelay | null = null;
  private streamStatus: StreamStatus = "idle";
  private isSigningIn = false;
  private isAudioEnabled = false;
  private isNekoOpen = false;
  private lastInputSeenAt = 0;
  private reportLocation: ((title: string) => void) | null = null;

  constructor(mount: HTMLElement) {
    this.root.className = "screen-root";
    this.strip = new Strip({
      onTakeControl: () => this.takeControl(),
      onReturnControl: () => this.returnControl(),
      onToggleAudio: () => this.setAudioEnabled(!this.isAudioEnabled),
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
    // A phone never resizes the Screen (spec D7); the shell gives pages no phone signal, so the shared query
    // the shell itself switches on decides.
    this.isPhone = window.matchMedia(PHONE_MEDIA_QUERY).matches;
    this.visibility = new ViewerVisibility(
      { onRelease: () => this.releaseMedia(), onRequest: () => this.requestMedia() },
      windowScheduler,
    );
    this.resizeReporter = new ResizeReporter((size) => this.sendScreenSize(size.width, size.height), windowScheduler);
    this.observeVisibility();
    this.observeStageSize();
    window.addEventListener("pagehide", () => this.logout());
  }

  attachShell(reportLocation: (title: string) => void): void {
    this.reportLocation = reportLocation;
    if (this.state) reportLocation(this.state.title);
  }

  shellShown(isShown: boolean): void {
    this.visibility.setShellShown(isShown);
    if (isShown) this.resizeReporter.forget();
    this.render();
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
    } else if (event.type === "profile") {
      this.stream?.selectVideo(event.video_id);
    } else if (event.type === "handoff_request") {
      // The strip already shows the queue; the overlay's dim lifts as the lease moved to nobody.
    }
  }

  private applyState(state: ScreenStateDocument): void {
    const previousTitle = this.state?.title;
    const previousVideoId = this.state?.video_id;
    this.state = state;
    if (state.title !== previousTitle) this.reportLocation?.(state.title);
    if (state.lifecycle === "running") {
      if (this.session === null) void this.signIn();
      else if (this.stream && state.video_id !== previousVideoId) this.stream.selectVideo(state.video_id);
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
    // A hidden viewer keeps its lease but sends nothing (spec section 8.2).
    const mayControl =
      (isController || this.state.control.controller === "nobody") && this.state.lifecycle === "running";
    const canSend = mayControl && this.visibility.isVisible;
    this.strip.update(this.state.control, ownSessionId, this.streamStatus);
    this.overlays.update(this.state, this.streamStatus, this.session !== null);
    this.input?.setEnabled(canSend);
    this.resizeReporter.setEnabled(isController && this.state.lifecycle === "running" && !this.isPhone);
    this.stage.classList.toggle("screen-stage--not-controlling", !mayControl);
    this.stage.dataset.screenController = isController ? "you" : this.state.control.controller;
    this.stage.dataset.screenVisible = String(this.visibility.isVisible);
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
      this.stream.setAudioEnabled(this.isAudioEnabled);
      this.input = new InputRelay(socket, this.stage, () => this.noteInput());
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
    this.isNekoOpen = true;
    // A reopened socket is a new neko connection with no peer: ask again if this viewer is visible.
    this.visibility.reset();
    this.visibility.evaluate();
  }

  /** The visibility machine says this viewer should hold media (spec section 8.2). */
  private requestMedia(): void {
    if (!this.stream || !this.state || !this.isNekoOpen) return;
    const videoId = this.state.video_id;
    fetchConnectInfo(false)
      .then((connectInfo) => this.stream?.request(videoId, connectInfo))
      .catch((error) => console.error("[screen] connect-info failed", error));
  }

  /** Hidden for the delay: the media goes, the socket, the session and the lease stay. */
  private releaseMedia(): void {
    this.stream?.release();
  }

  private observeVisibility(): void {
    document.addEventListener("visibilitychange", () => {
      this.visibility.setDocumentHidden(document.hidden);
      this.render();
    });
    this.visibility.setDocumentHidden(document.hidden);
    if (typeof IntersectionObserver !== "undefined") {
      new IntersectionObserver(
        (entries) => {
          const last = entries[entries.length - 1];
          if (last) {
            this.visibility.setIntersecting(last.isIntersecting);
            this.render();
          }
        },
        { threshold: [0, 0.01] },
      ).observe(this.stage);
    }
    // The belt: a display:none flip or a frame the observer missed is caught within the interval.
    window.setInterval(() => {
      const rect = this.stage.getBoundingClientRect();
      const isLaidOut = rect.width > 4 && rect.height > 4;
      if (!isLaidOut) this.visibility.setIntersecting(false);
      this.visibility.setDocumentHidden(document.hidden);
      this.visibility.evaluate();
      this.render();
    }, REEVALUATE_INTERVAL_MS);
  }

  private observeStageSize(): void {
    if (typeof ResizeObserver === "undefined") return;
    new ResizeObserver((entries) => {
      const rect = entries[entries.length - 1]?.contentRect;
      if (rect) this.resizeReporter.observe(rect.width, rect.height);
    }).observe(this.stage);
  }

  private sendScreenSize(width: number, height: number): void {
    if (this.session === null) return;
    postScreenSize(width, height, this.session.session_id).catch((error) =>
      console.error("[screen] screen-size failed", error),
    );
  }

  private noteInput(): void {
    if (this.session === null) return;
    const now = performance.now();
    if (now - this.lastInputSeenAt < INPUT_SEEN_THROTTLE_MS) return;
    this.lastInputSeenAt = now;
    postInputSeen(this.session.session_id).catch(() => undefined);
  }

  private setAudioEnabled(isEnabled: boolean): void {
    this.isAudioEnabled = isEnabled;
    this.strip.setAudioEnabled(isEnabled);
    this.stream?.setAudioEnabled(isEnabled);
    if (!this.stream) this.video.muted = !isEnabled;
  }

  private teardownStream(): void {
    this.stream?.close();
    this.socket?.stop();
    this.stream = null;
    this.socket = null;
    this.input = null;
    this.session = null;
    this.isNekoOpen = false;
    this.streamStatus = "idle";
    this.visibility.reset();
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
    // The new controller's stage decides the Screen's size from here.
    this.resizeReporter.forget();
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
    onShown: () => viewer.shellShown(true),
    onHidden: () => viewer.shellShown(false),
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
