/** The strip above the stage (spec section 4.2): who has control, Take control, Give control back, the queue. */

import type { ControlDocument } from "./state";
import { describeController, isOwnSessionController } from "./state";

export interface StripActions {
  onTakeControl: () => void;
  onReturnControl: () => void;
}

export class Strip {
  readonly element = document.createElement("div");
  private readonly who = document.createElement("span");
  private readonly waiting = document.createElement("span");
  private readonly status = document.createElement("span");
  private readonly takeButton = document.createElement("button");
  private readonly returnButton = document.createElement("button");

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
    this.element.append(this.who, this.waiting, this.status, this.takeButton, this.returnButton);
  }

  update(control: ControlDocument, ownSessionId: string | null, streamStatus: string, notice: string | null): void {
    const isOwn = isOwnSessionController(control, ownSessionId);
    this.who.textContent = `Control: ${describeController(control, ownSessionId)}`;
    this.waiting.textContent = control.waiting.length > 0 ? `Waiting: ${control.waiting.join(", ")}` : "";
    // A refused verb's reason takes the status's place for a moment, so a click that did nothing says why.
    this.status.textContent = notice ?? (streamStatus === "connected" ? "" : streamStatus);
    this.status.classList.toggle("screen-strip__status--notice", notice !== null);
    this.takeButton.hidden = isOwn || ownSessionId === null;
    // Giving control back is offered when this human holds it and an agent waits to resume.
    this.returnButton.hidden = !(isOwn && control.waiting.length > 0);
  }
}
