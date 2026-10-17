/** The overlays over the stage: installing, starting, connecting, stopped (with Start), and the agent dim. */

import type { ControlDocument, ScreenStateDocument } from "./state";
import type { StreamStatus } from "./webrtc";

export class Overlays {
  readonly element = document.createElement("div");
  private readonly title = document.createElement("div");
  private readonly detail = document.createElement("div");
  private readonly startButton = document.createElement("button");
  private readonly dim = document.createElement("div");
  private readonly dimTitle = document.createElement("div");
  private readonly dimTake = document.createElement("button");

  constructor(onStart: () => void, onTakeControl: () => void) {
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
    this.dimTake.className = "screen-button screen-button--primary";
    this.dimTake.textContent = "Take control";
    this.dimTake.addEventListener("click", () => onTakeControl());
    this.dim.append(this.dimTitle, this.dimTake);
  }

  get dimElement(): HTMLElement {
    return this.dim;
  }

  update(state: ScreenStateDocument, streamStatus: StreamStatus, hasSession: boolean): void {
    const control: ControlDocument = state.control;
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
        if (streamStatus === "connected") this.element.hidden = true;
        else if (streamStatus === "failed") this.show("The stream could not connect", "", false);
        else this.show(hasSession ? "Connecting to the screen" : "Signing in to the screen", "", false);
        break;
    }
    const isAgent = control.controller === "agent" && state.lifecycle === "running";
    this.dim.hidden = !isAgent;
    if (isAgent) this.dimTitle.textContent = `${control.agent_name ?? control.agent_id ?? "An agent"} has control`;
  }

  private show(title: string, detail: string, isStartOffered: boolean): void {
    this.element.hidden = false;
    this.title.textContent = title;
    this.detail.textContent = detail;
    this.startButton.hidden = !isStartOffered;
  }
}
