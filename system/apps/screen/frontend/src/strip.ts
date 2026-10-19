/** The strip above the stage (spec sections 4.2, 4.3 and 5.4): who has control, Take control, Give control back, the
 *  queue, the media path's status (loud when the picture is the screencast, with Retry), a Details toggle for the
 *  path's particulars, the mute control (audio is off per session until unmuted), and the pointer-lock control where
 *  it applies (spec section 12). */

import type { ControlDocument } from "./state";
import { describeController, isOwnSessionController } from "./state";
import type { StreamStatus } from "./webrtc";

/** What the strip says about the media path (spec sections 4.2 and 5.4). */
export function describeStreamStatus(status: StreamStatus): string {
  switch (status) {
    case "connected":
    case "idle":
      return "";
    case "connecting":
      return "Connecting";
    case "screencast":
      return "Low-quality picture: no direct or relay path connected. Retrying.";
    case "failed":
      return "The stream could not connect";
  }
}

export interface StripActions {
  onTakeControl: () => void;
  onReturnControl: () => void;
  /** The pointer-lock control's button, offered while this human holds control. */
  pointerLockButton: HTMLButtonElement;
  onToggleAudio: () => void;
  /** Another WebRTC try from the screencast, now. */
  onRetry: () => void;
  /** The Details panel opened (true) or closed. */
  onToggleDetails: (isOpen: boolean) => void;
}

export class Strip {
  readonly element = document.createElement("div");
  private readonly who = document.createElement("span");
  private readonly waiting = document.createElement("span");
  private readonly status = document.createElement("span");
  private readonly alert = document.createElement("span");
  private readonly alertText = document.createElement("span");
  private readonly retryButton = document.createElement("button");
  private readonly detailsButton = document.createElement("button");
  private readonly takeButton = document.createElement("button");
  private readonly returnButton = document.createElement("button");
  private readonly muteButton = document.createElement("button");
  private transientStatus = "";
  private streamStatusText = "";
  private isDetailsOpen = false;

  constructor(actions: StripActions) {
    this.element.className = "screen-strip";
    this.element.dataset.screenStrip = "true";
    this.who.className = "screen-strip__who";
    this.who.dataset.screenController = "true";
    this.waiting.className = "screen-strip__waiting";
    this.status.className = "screen-strip__status";
    this.alert.className = "screen-strip__alert";
    this.alert.dataset.screenAlert = "true";
    this.alert.hidden = true;
    this.alertText.className = "screen-strip__alert-text";
    this.retryButton.className = "screen-button screen-button--alert";
    this.retryButton.textContent = "Retry now";
    this.retryButton.dataset.screenRetry = "true";
    this.retryButton.addEventListener("click", () => actions.onRetry());
    this.alert.append(this.alertText, this.retryButton);
    this.detailsButton.className = "screen-button screen-button--quiet";
    this.detailsButton.textContent = "Details";
    this.detailsButton.title = "How this window is connected to the Screen";
    this.detailsButton.dataset.screenDetails = "true";
    this.detailsButton.setAttribute("aria-pressed", "false");
    this.detailsButton.addEventListener("click", () => {
      this.isDetailsOpen = !this.isDetailsOpen;
      this.detailsButton.setAttribute("aria-pressed", String(this.isDetailsOpen));
      actions.onToggleDetails(this.isDetailsOpen);
    });
    this.takeButton.className = "screen-button screen-button--primary";
    this.takeButton.textContent = "Take control";
    this.takeButton.dataset.screenTakeControl = "true";
    this.takeButton.addEventListener("click", () => actions.onTakeControl());
    this.returnButton.className = "screen-button";
    this.returnButton.textContent = "Give control back";
    this.returnButton.dataset.screenReturnControl = "true";
    this.returnButton.addEventListener("click", () => actions.onReturnControl());
    this.muteButton.className = "screen-button screen-button--mute";
    this.muteButton.dataset.screenMute = "true";
    this.muteButton.addEventListener("click", () => actions.onToggleAudio());
    this.setAudioEnabled(false);
    this.element.append(
      this.who,
      this.waiting,
      this.alert,
      this.status,
      this.detailsButton,
      actions.pointerLockButton,
      this.takeButton,
      this.returnButton,
      this.muteButton,
    );
  }

  setAudioEnabled(isEnabled: boolean): void {
    this.muteButton.textContent = isEnabled ? "Mute" : "Unmute";
    this.muteButton.setAttribute("aria-pressed", String(isEnabled));
    this.muteButton.title = isEnabled
      ? "Mute the Screen's audio for this window"
      : "Play the Screen's audio in this window";
  }

  /** A transient line (a clipboard or drop result) shown in place of the stream status; "" clears it. */
  showStatus(message: string): void {
    this.transientStatus = message;
    this.status.textContent = message || this.streamStatusText;
  }

  update(
    control: ControlDocument,
    ownSessionId: string | null,
    streamStatus: StreamStatus,
    notice: string | null,
  ): void {
    const isOwn = isOwnSessionController(control, ownSessionId);
    this.who.textContent = `Control: ${describeController(control, ownSessionId)}`;
    this.waiting.textContent = control.waiting.length > 0 ? `Waiting: ${control.waiting.join(", ")}` : "";
    // The screencast's status is the alert, in the same row, loud and with Retry; the other statuses are the quiet
    // line. A refused verb's reason takes the quiet line's place for a moment, so a click that did nothing says
    // why; otherwise a transient line (a clipboard or drop result) stands in for the stream status.
    const isScreencast = streamStatus === "screencast";
    this.alert.hidden = !isScreencast;
    this.alertText.textContent = isScreencast ? describeStreamStatus(streamStatus) : "";
    this.streamStatusText = isScreencast ? "" : describeStreamStatus(streamStatus);
    this.status.textContent = notice ?? (this.transientStatus || this.streamStatusText);
    this.status.classList.toggle("screen-strip__status--notice", notice !== null);
    this.status.dataset.screenStreamStatus = streamStatus;
    this.takeButton.hidden = isOwn || ownSessionId === null;
    // Giving control back is offered when this human holds it and an agent waits to resume.
    this.returnButton.hidden = !(isOwn && control.waiting.length > 0);
  }
}
