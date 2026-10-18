/** The overlays over the stage: installing, starting, connecting, stopped (with Start), and the agent dim, which
 *  also names a page the human asked to open while the agent drives (spec section 4.2): Take control opens it,
 *  Cancel drops it (the owner's alone, as the route that drops it refuses a visitor). */

import type { ControlDocument, NekoSessionAnswer, ScreenStateDocument } from "./state";
import type { StreamStatus } from "./webrtc";

export class Overlays {
  readonly element = document.createElement("div");
  private readonly title = document.createElement("div");
  private readonly detail = document.createElement("div");
  private readonly startButton = document.createElement("button");
  private readonly dim = document.createElement("div");
  private readonly dimTitle = document.createElement("div");
  private readonly dimPending = document.createElement("div");
  private readonly dimTake = document.createElement("button");
  private readonly dimCancel = document.createElement("button");

  constructor(onStart: () => void, onTakeControl: () => void, onCancelPendingUrl: () => void) {
    this.element.className = "screen-overlay";
    this.element.dataset.screenOverlay = "true";
    this.title.className = "screen-overlay__title";
    this.detail.className = "screen-overlay__detail";
    this.startButton.className = "screen-button screen-button--primary";
    this.startButton.textContent = "Start";
    this.startButton.dataset.screenStart = "true";
    this.startButton.addEventListener("click", () => onStart());
    this.element.append(this.title, this.detail, this.startButton);
    this.dim.className = "screen-overlay screen-overlay--dim";
    this.dim.dataset.screenAgentOverlay = "true";
    this.dimTitle.className = "screen-overlay__title";
    this.dimPending.className = "screen-overlay__detail";
    this.dimPending.dataset.screenPendingUrl = "true";
    this.dimTake.className = "screen-button screen-button--primary";
    this.dimTake.textContent = "Take control";
    this.dimTake.addEventListener("click", () => onTakeControl());
    this.dimCancel.className = "screen-button";
    this.dimCancel.textContent = "Cancel";
    this.dimCancel.dataset.screenCancelPendingUrl = "true";
    this.dimCancel.addEventListener("click", () => onCancelPendingUrl());
    this.dim.append(this.dimTitle, this.dimPending, this.dimTake, this.dimCancel);
  }

  get dimElement(): HTMLElement {
    return this.dim;
  }

  update(
    state: ScreenStateDocument,
    streamStatus: StreamStatus,
    session: NekoSessionAnswer | null,
    signInError: string | null,
  ): void {
    const control: ControlDocument = state.control;
    const hasSession = session !== null;
    switch (state.lifecycle) {
      case "installing":
        this.show("Installing the screen", state.install_reason ?? "", false);
        break;
      case "starting":
        this.show("Starting the screen", "", false);
        break;
      case "stopped":
        this.show("The screen was stopped", state.stop_reason ?? "", true);
        break;
      case "running":
        if (streamStatus === "connected" || streamStatus === "screencast") this.element.hidden = true;
        else if (streamStatus === "failed") this.show("The stream could not connect", "", false);
        else if (hasSession) this.show("Connecting to the screen", "", false);
        else if (signInError !== null)
          this.show("Could not sign in to the screen", `${signInError} (retrying)`, false);
        else this.show("Signing in to the screen", "", false);
        break;
    }
    const isAgent = control.controller === "agent" && state.lifecycle === "running";
    this.dim.hidden = !isAgent;
    if (isAgent) this.dimTitle.textContent = `${control.agent_name ?? control.agent_id ?? "An agent"} has control`;
    const pendingUrl = state.chromium.pending_url;
    const hasPending = isAgent && pendingUrl !== null;
    this.dimPending.hidden = !hasPending;
    this.dimCancel.hidden = !(hasPending && session?.is_owner === true);
    if (hasPending) {
      this.dimPending.textContent = `Waiting to open ${pendingUrl} in the browser: take control to open it now`;
      this.dimTake.textContent = "Take control and open it";
    } else {
      this.dimTake.textContent = "Take control";
    }
  }

  private show(title: string, detail: string, isStartOffered: boolean): void {
    this.element.hidden = false;
    this.title.textContent = title;
    this.detail.textContent = detail;
    this.startButton.hidden = !isStartOffered;
  }
}
