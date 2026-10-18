/**
 * The Screen's viewer (docs/system/specs/screen/spec.md section 11): the live desktop in a video element, the
 * strip naming who has control, the overlays for a stack that is installing, starting or stopped, and input sent
 * to neko while this viewer's session is the controller and the viewer is visible. It connects to the shell that
 * frames it, reporting ``/`` with the title the service tracks, and owns the close chord (Ctrl+W goes to the
 * remote). The controller's stage size drives the Screen's size (section 7.2), the service's profile drives the
 * video every viewer selects (section 8.1), a hidden viewer releases its media (section 8.2), and audio is off
 * until unmuted (section 8.3). The clipboard crosses both ways while this viewer controls (section 13), a dropped
 * file lands on the Screen at the pointer (section 4.3), and the phone layout pans, zooms and types through its
 * own controls (section 14). The media path is rewritten to the endpoints ``connect-info`` names and escalates
 * to a relay, then to the JPEG screencast, when no pair connects (section 5).
 */

import "./style.css";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { createContextMenuOpener } from "@imbue/workspace-ui/src/components/contextMenuOpener";
import { installElementContextMenu } from "@imbue/workspace-ui/src/context_menu";
import { PHONE_MEDIA_QUERY } from "@imbue/workspace-ui/src/device_queries";
import { ClipboardBridge, navigatorClipboard, serviceClipboardTransport } from "./clipboard";
import { uploadDroppedFiles } from "./files";
import { InputRelay, XK_V } from "./input";
import { keyboardMapForLanguage } from "./keyboard_map";
import { MobileControls } from "./mobile";
import { NekoSocket } from "./neko_socket";
import { Overlays } from "./overlays";
import { PointerLockControl } from "./pointer_lock";
import { pageStorage, readNeedsRelay } from "./relay_memory";
import { ResizeReporter } from "./resize_reporter";
import { windowScheduler } from "./scheduler";
import { ScreencastPoller } from "./screencast";
import {
  ServiceEvents,
  ServiceRequestError,
  fetchConnectInfo,
  fetchState,
  postCancelPendingUrl,
  postInputSeen,
  postReturnControl,
  postScreenSize,
  postSession,
  postStart,
  postTakeControl,
  type VerbOutcome,
} from "./service_client";
import type { NekoSessionAnswer, ScreenStateDocument, ServiceEvent } from "./state";
import { isOwnSessionController } from "./state";
import { Strip } from "./strip";
import { REEVALUATE_INTERVAL_MS, ViewerVisibility } from "./visibility";
import { NekoStream, type StreamStatus } from "./webrtc";

export const PAGE_PATH = "/";

// How long a refused sign-in waits before trying again, and how long a refused verb's reason stays in the strip.
const SIGN_IN_RETRY_MS = 3000;
const NOTICE_MS = 4000;
/** How often at most the input relay tells the service this session is active (its window is ten seconds). */
const INPUT_SEEN_THROTTLE_MS = 2000;
const CLIPBOARD_STATUS_MS = 2200;

class Viewer {
  private readonly root = document.createElement("div");
  private readonly stage = document.createElement("div");
  private readonly video = document.createElement("video");
  private readonly cast = document.createElement("img");
  private screencast: ScreencastPoller | null = null;
  private readonly strip: Strip;
  private readonly overlays: Overlays;
  private readonly pointerLock: PointerLockControl;
  private readonly visibility: ViewerVisibility;
  private readonly resizeReporter: ResizeReporter;
  private readonly clipboard: ClipboardBridge;
  private readonly mobile: MobileControls | null;
  private readonly isPhone: boolean;
  private clipboardStatusTimer = 0;
  private state: ScreenStateDocument | null = null;
  private session: NekoSessionAnswer | null = null;
  private socket: NekoSocket | null = null;
  private stream: NekoStream | null = null;
  private input: InputRelay | null = null;
  private streamStatus: StreamStatus = "idle";
  private isSigningIn = false;
  private signInError: string | null = null;
  private signInRetryTimer = 0;
  private notice: string | null = null;
  private noticeTimer = 0;
  private isAudioEnabled = false;
  private isNekoOpen = false;
  private lastInputSeenAt = 0;
  private reportLocation: ((title: string) => void) | null = null;

  constructor(mount: HTMLElement) {
    this.root.className = "screen-root";
    // The deltas have no neko event to go to yet (pointer_lock.ts): the control stays hidden and they are dropped.
    this.pointerLock = new PointerLockControl({ stage: this.stage, onRelativeMove: () => undefined });
    this.strip = new Strip({
      onTakeControl: () => this.takeControl(),
      onReturnControl: () => this.returnControl(),
      pointerLockButton: this.pointerLock.button,
      onToggleAudio: () => this.setAudioEnabled(!this.isAudioEnabled),
    });
    this.overlays = new Overlays(
      () => void postStart(),
      () => this.takeControl(),
      () => this.cancelPendingUrl(),
    );
    this.stage.className = "screen-stage screen-stage--not-controlling";
    this.stage.tabIndex = 0;
    this.stage.dataset.screenStage = "true";
    this.video.autoplay = true;
    this.video.playsInline = true;
    this.video.muted = true;
    this.cast.className = "screen-cast";
    this.cast.alt = "";
    this.cast.hidden = true;
    this.cast.dataset.screenScreencast = "true";
    this.stage.append(this.video, this.cast, this.overlays.dimElement, this.overlays.element);
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
    this.clipboard = new ClipboardBridge(
      serviceClipboardTransport(() => this.session?.token ?? null),
      navigatorClipboard,
      (message) => this.showClipboardStatus(message),
    );
    this.mobile = this.isPhone ? this.buildMobileControls() : null;
    this.observeVisibility();
    this.observeStageSize();
    this.wireClipboard();
    this.wireFileDrop();
    window.addEventListener("pagehide", () => this.logout());
  }

  private buildMobileControls(): MobileControls {
    this.root.classList.add("screen-root--phone");
    const keys = {
      keyDown: (keysym: number) => this.input?.keyDown(keysym),
      keyUp: (keysym: number) => this.input?.keyUp(keysym),
    };
    const controls = new MobileControls(
      this.stage,
      this.video,
      {
        clickAt: (clientX, clientY, button) => this.input?.clickAt(clientX, clientY, button),
        scroll: (steps) => this.input?.scroll(steps),
      },
      keys,
      windowScheduler,
    );
    this.root.append(controls.bar);
    return controls;
  }

  private wireClipboard(): void {
    window.addEventListener("focus", () => void this.clipboard.onFocus());
    document.addEventListener("paste", (event) => {
      if (!this.clipboard.isActive || !event.clipboardData) return;
      event.preventDefault();
      void this.clipboard.onPaste(event.clipboardData).then((isSent) => {
        // The clipboard is on the Screen: paste it there with the chord the user pressed (Ctrl+V, or synthesized
        // for a Cmd+V), unless nothing crossed.
        if (isSent) this.input?.tapWithControl(XK_V);
      });
    });
  }

  private wireFileDrop(): void {
    this.stage.addEventListener("dragover", (event) => {
      if (!this.canUpload()) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      this.stage.classList.add("screen-stage--dropping");
    });
    this.stage.addEventListener("dragleave", () => this.stage.classList.remove("screen-stage--dropping"));
    this.stage.addEventListener("drop", (event) => {
      this.stage.classList.remove("screen-stage--dropping");
      if (!this.canUpload() || !event.dataTransfer || this.session === null || !this.input) return;
      event.preventDefault();
      const files = Array.from(event.dataTransfer.files);
      const at = this.input.pointAt(event.clientX, event.clientY);
      if (files.length === 0 || at === null) return;
      this.showClipboardStatus(
        files.length === 1 ? "Dropping the file on the Screen" : "Dropping the files on the Screen",
      );
      uploadDroppedFiles(files, at, this.session.token)
        .then((isUploaded) => this.showClipboardStatus(isUploaded ? "Dropped on the Screen" : "The drop failed"))
        .catch(() => this.showClipboardStatus("The drop failed"));
    });
  }

  private canUpload(): boolean {
    return (
      this.state !== null &&
      this.session !== null &&
      isOwnSessionController(this.state.control, this.session.session_id)
    );
  }

  private showClipboardStatus(message: string): void {
    this.strip.showStatus(message);
    window.clearTimeout(this.clipboardStatusTimer);
    this.clipboardStatusTimer = window.setTimeout(() => this.strip.showStatus(""), CLIPBOARD_STATUS_MS);
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
    } else if (event.type === "clipboard") {
      if (event.kind === "image") void this.clipboard.onRemoteImageUpdated();
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
    this.strip.update(this.state.control, ownSessionId, this.streamStatus, this.notice);
    this.pointerLock.setAvailable(isController);
    this.overlays.update(this.state, this.streamStatus, this.session, this.signInError);
    this.input?.setEnabled(canSend);
    this.mobile?.setEnabled(canSend);
    this.clipboard.isActive = isController && this.state.lifecycle === "running";
    this.resizeReporter.setEnabled(isController && this.state.lifecycle === "running" && !this.isPhone);
    this.stage.classList.toggle("screen-stage--not-controlling", !mayControl);
    this.stage.dataset.screenController = isController ? "you" : this.state.control.controller;
    this.stage.dataset.screenVisible = String(this.visibility.isVisible);
  }

  private async signIn(): Promise<void> {
    if (this.isSigningIn) return;
    this.isSigningIn = true;
    window.clearTimeout(this.signInRetryTimer);
    try {
      let session: NekoSessionAnswer;
      try {
        session = await postSession();
      } catch (error) {
        this.noteSignInFailure(error);
        return;
      }
      this.signInError = null;
      this.session = session;
      const socket = new NekoSocket(session.token, () => this.onNekoOpen());
      this.socket = socket;
      this.stream = new NekoStream(socket, this.video, (status) => {
        this.streamStatus = status;
        this.syncScreencast();
        this.render();
      });
      this.stream.setAudioEnabled(this.isAudioEnabled);
      this.input = new InputRelay(socket, this.stage, () => this.noteInput());
      if (this.mobile) this.input.viewTransform = () => this.mobile?.viewTransform ?? { zoom: 1, pan: { x: 0, y: 0 } };
      if (this.state?.screen_size) this.input.screen = this.state.screen_size;
      socket.on("clipboard/updated", () => void this.clipboard.onRemoteTextUpdated());
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

  /** A refused sign-in is shown on the overlay and tried again while the stack still reads running. */
  private noteSignInFailure(error: unknown): void {
    this.signInError = error instanceof ServiceRequestError ? error.detail : String(error);
    console.warn("[screen] sign-in failed:", this.signInError);
    this.render();
    this.signInRetryTimer = window.setTimeout(() => {
      if (this.session === null && this.state?.lifecycle === "running") void this.signIn();
    }, SIGN_IN_RETRY_MS);
  }

  /** What a refused verb said, in the strip for a moment. */
  private showNotice(outcome: VerbOutcome): void {
    if (outcome.ok) return;
    this.notice = outcome.detail ?? "refused";
    window.clearTimeout(this.noticeTimer);
    this.noticeTimer = window.setTimeout(() => {
      this.notice = null;
      this.render();
    }, NOTICE_MS);
    this.render();
  }

  private onNekoOpen(): void {
    this.isNekoOpen = true;
    // neko maps keysyms with the layout it is told, once per session (spec section 13); only the host may set it,
    // so it is sent again when this session takes control.
    this.sendKeyboardMap();
    // A reopened socket is a new neko connection with no peer: ask again if this viewer is visible.
    this.visibility.reset();
    this.visibility.evaluate();
  }

  /** The visibility machine says this viewer should hold media (spec section 8.2). */
  private requestMedia(): void {
    if (!this.stream || !this.state || !this.isNekoOpen) return;
    const videoId = this.state.video_id;
    // A network that needed a relay last time gets the relay servers up front (spec section 5.3).
    const isRelay = readNeedsRelay(pageStorage());
    fetchConnectInfo(isRelay)
      .then((connectInfo) => {
        // A release while the fetch was in flight means a hidden viewer: it asks for nothing until seen again.
        if (this.visibility.wantsMedia) this.stream?.request(videoId, connectInfo, isRelay);
      })
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

  private sendKeyboardMap(): void {
    const choice = keyboardMapForLanguage(navigator.language ?? "");
    this.socket?.send("keyboard/map", { layout: choice.layout, variant: choice.variant });
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

  /** The screencast runs exactly while the stream says no media path connected (spec section 5.4). */
  private syncScreencast(): void {
    const wanted = this.streamStatus === "screencast" && this.session !== null;
    if (wanted && this.screencast === null && this.session !== null) {
      this.screencast = new ScreencastPoller(this.cast, this.session.token);
      this.screencast.start();
    } else if (!wanted && this.screencast !== null) {
      this.screencast.stop();
      this.screencast = null;
    }
  }

  private teardownStream(): void {
    window.clearTimeout(this.signInRetryTimer);
    this.signInError = null;
    this.screencast?.stop();
    this.screencast = null;
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
    this.showNotice(await postTakeControl(this.session.session_id));
    // The new controller's stage decides the Screen's size from here, and its layout the keyboard map.
    this.resizeReporter.forget();
    this.sendKeyboardMap();
    this.stage.focus();
  }

  private async returnControl(): Promise<void> {
    if (this.session === null) return;
    this.showNotice(await postReturnControl(this.session.session_id));
  }

  private cancelPendingUrl(): void {
    const name = this.state?.chromium.name;
    if (!name) return;
    void postCancelPendingUrl(name);
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
