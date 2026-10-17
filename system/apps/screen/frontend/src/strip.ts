/** The strip above the stage (spec sections 4.2 and 4.3): who has control, Take control, Give control back, the
 *  queue, the mute control (audio is off per session until unmuted), and the pointer-lock control where it
 *  applies (spec section 12). */

import type { ControlDocument } from "./state";
import { describeController, isOwnSessionController } from "./state";

export interface StripActions {
  onTakeControl: () => void;
  onReturnControl: () => void;
  /** The pointer-lock control's button, offered while this human holds control. */
  pointerLockButton: HTMLButtonElement;
  onToggleAudio: () => void;
}

export class Strip {
  readonly element = document.createElement("div");
  private readonly who = document.createElement("span");
  private readonly waiting = document.createElement("span");
  private readonly status = document.createElement("span");
  private readonly takeButton = document.createElement("button");
  private readonly returnButton = document.createElement("button");
  private readonly muteButton = document.createElement("button");
  private transientStatus = "";
  private streamStatusText = "";

  constructor(actions: StripActions) {
    this.element.className = "screen-strip";
    this.element.dataset.screenStrip = "true";
    this.who.className = "screen-strip__who";
    this.who.dataset.screenController = "true";
    this.waiting.className = "screen-strip__waiting";
    this.status.className = "screen-strip__status";
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
      this.status,
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

  update(control: ControlDocument, ownSessionId: string | null, streamStatus: string, notice: string | null): void {
    const isOwn = isOwnSessionController(control, ownSessionId);
    this.who.textContent = `Control: ${describeController(control, ownSessionId)}`;
    this.waiting.textContent = control.waiting.length > 0 ? `Waiting: ${control.waiting.join(", ")}` : "";
    // A refused verb's reason takes the status's place for a moment, so a click that did nothing says why;
    // otherwise a transient line (a clipboard or drop result) stands in for the stream status.
    this.streamStatusText = streamStatus === "connected" ? "" : streamStatus;
    this.status.textContent = notice ?? (this.transientStatus || this.streamStatusText);
    this.status.classList.toggle("screen-strip__status--notice", notice !== null);
    this.takeButton.hidden = isOwn || ownSessionId === null;
    // Giving control back is offered when this human holds it and an agent waits to resume.
    this.returnButton.hidden = !(isOwn && control.waiting.length > 0);
  }
}
