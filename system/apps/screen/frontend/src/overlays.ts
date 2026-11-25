/** The overlays over the stage: setting up (or failed to set up), starting, connecting, stopped (with Start), and
 *  the agent dim, which also names a page the human asked to open while the agent drives (spec section 4.2): Take
 *  control opens it, Cancel drops it, offered to the sessions the service lets drop it.
 *
 *  A failure says what happened in a sentence of ours, keeps the service's own text behind "See details", and offers
 *  what can help: Start, and "Ask an agent to fix this" where a shell frames the page (it sends the ask to the chat
 *  about the Screen). "Report a bug" is the fallback, as in workspace_ui's FailureRemedies: offered where the shell's
 *  handshake says Imbue Studio opens one, once an ask was refused (beside "Draft it in a chat", which leaves the same
 *  request unsent in a chat's message box) or for a failure that owes help where no agent can be asked. */

import { askAgentToFix, withAskFailure } from "@imbue/workspace-ui/src/app_contract";
import type {
  AgentFixRequest,
  ReportedFailure,
  ShellConnection,
  ShownFailure,
  StartOutcome,
} from "@imbue/workspace-ui/src/app_contract";
import {
  ASKING_AN_AGENT_LABEL,
  ASK_AGENT_TO_FIX_LABEL,
  DRAFT_ASK_LABEL,
} from "@imbue/workspace-ui/src/components/FailureButtons";
import { REPORT_A_BUG_LABEL, fallbackSentence, failureSentence } from "@imbue/workspace-ui/src/models/failure";
import type { Failure, FailureMessage } from "@imbue/workspace-ui/src/models/failure";
import type { ControlDocument, NekoSessionAnswer, ScreenStateDocument } from "./state";
import { isOwnSessionController } from "./state";
import type { StreamStatus } from "./webrtc";

/** What the overlay needs of the shell connection for its failure buttons. */
export type FailureHost = Pick<ShellConnection, "hasShell" | "askAgent" | "canReportBug" | "reportBug">;

/** What the main overlay shows: hidden (null), or a title, a line under it, the details, and the offered actions. */
export interface OverlayView {
  readonly title: string;
  readonly body: string;
  readonly details: string | null;
  /** The Start button's label, or null when it is not offered. */
  readonly startLabel: string | null;
  /** Whether "Try again" loads the Screen's state again (the page never got one). */
  readonly isReloadOffered: boolean;
  /** What "Ask an agent to fix this" hands the agent, or null when it is not offered. */
  readonly agentFix: AgentFixRequest | null;
  /** Whether "Report a bug" stands in for the agent where none can be asked. */
  readonly isHelpOwed: boolean;
}

const INSTALL_FAILED_STEM = "The Screen couldn't be set up in this workspace";
/** What failed when the stack would not start, or a Start was refused or not answered. */
export const START_FAILED_STEM = "The Screen couldn't start";
const STOPPED_UNEXPECTEDLY_STEM = "The Screen stopped unexpectedly";
const LOAD_FAILED_STEM = "The Screen couldn't load";

function joinDetails(parts: readonly (string | null)[]): string | null {
  const kept = parts.filter((part): part is string => part !== null && part.trim() !== "");
  return kept.length === 0 ? null : kept.join("\n");
}

function view(title: string, fields: Partial<Omit<OverlayView, "title">> = {}): OverlayView {
  return {
    title,
    body: "",
    details: null,
    startLabel: null,
    isReloadOffered: false,
    agentFix: null,
    isHelpOwed: false,
    ...fields,
  };
}

/** What the main overlay shows when the page could not load the Screen's state at all. */
export function describeLoadFailure(failure: Failure): OverlayView {
  const told = failureSentence(failure, LOAD_FAILED_STEM);
  const fields = {
    details: told.details,
    isReloadOffered: true,
    agentFix: told.isHelpOwed ? { whatFailed: LOAD_FAILED_STEM, details: told.details } : null,
    isHelpOwed: told.isHelpOwed,
  };
  // These sentences do not name the Screen, so they go under a title that does.
  if (failure.cause === "not_answered" || failure.cause === "user_facing") {
    return view(`${LOAD_FAILED_STEM}.`, { ...fields, body: told.message });
  }
  return view(told.message, fields);
}

function stoppedView(state: ScreenStateDocument, startFailure: FailureMessage | null): OverlayView {
  const details = state.stop_reason;
  let stopped: OverlayView;
  switch (state.stop_cause) {
    case "start_failed":
      stopped = view(fallbackSentence(START_FAILED_STEM), {
        details,
        startLabel: "Try again",
        agentFix: { whatFailed: START_FAILED_STEM, details },
        isHelpOwed: true,
      });
      break;
    case "crashed":
      stopped = view(`${STOPPED_UNEXPECTEDLY_STEM}.`, {
        details,
        startLabel: "Start",
        agentFix: { whatFailed: STOPPED_UNEXPECTEDLY_STEM, details },
        isHelpOwed: true,
      });
      break;
    case "out_of_memory":
      stopped = view(`${STOPPED_UNEXPECTEDLY_STEM}.`, {
        body: "The workspace ran low on memory, so the Screen was stopped to keep everything else running.",
        details,
        startLabel: "Start",
        agentFix: { whatFailed: `${STOPPED_UNEXPECTEDLY_STEM}: the workspace ran low on memory`, details },
      });
      break;
    case "requested":
    case null:
      stopped = view("The Screen was stopped.", { details, startLabel: "Start" });
      break;
  }
  // A Start that was refused or not answered says so under the title, its own details first.
  if (startFailure === null) return stopped;
  const joined = joinDetails([startFailure.details, details]);
  return {
    ...stopped,
    // A refused Start after a failed start would repeat the title word for word.
    body: startFailure.message === stopped.title ? stopped.body : startFailure.message,
    details: joined,
    agentFix:
      stopped.agentFix ?? (startFailure.isHelpOwed ? { whatFailed: START_FAILED_STEM, details: joined } : null),
    isHelpOwed: stopped.isHelpOwed || startFailure.isHelpOwed,
  };
}

/** What the main overlay shows for the stack's state and this viewer's connection to it; null hides it. */
export function describeOverlay(
  state: ScreenStateDocument,
  streamStatus: StreamStatus,
  session: NekoSessionAnswer | null,
  signInFailureDetails: string | null,
  startFailure: FailureMessage | null,
): OverlayView | null {
  switch (state.lifecycle) {
    case "installing":
      if (state.install_failure !== null) {
        const details = joinDetails([state.install_reason, state.install_failure]);
        return view(`${INSTALL_FAILED_STEM}.`, {
          body: "Setup runs again the next time the workspace starts.",
          details,
          agentFix: { whatFailed: INSTALL_FAILED_STEM, details },
        });
      }
      return view("The Screen is still being set up in this workspace.", {
        body: "It starts on its own once it's ready; the first time can take a few minutes.",
        details: state.install_reason,
      });
    case "starting":
      return view("Starting the Screen");
    case "stopped":
      return stoppedView(state, startFailure);
    case "running":
      if (streamStatus === "connected" || streamStatus === "screencast") return null;
      if (streamStatus === "failed") return view("The stream could not connect");
      if (session !== null) return view("Connecting to the Screen");
      if (signInFailureDetails !== null) {
        return view("Couldn't connect to the Screen. Retrying…", { details: signInFailureDetails });
      }
      return view("Signing in to the Screen");
  }
}

/** Whether the service lets this session drop a held page: the session in control, or the owner's. */
export function mayCancelPendingUrl(control: ControlDocument, session: NekoSessionAnswer | null): boolean {
  if (session === null) return false;
  return session.is_owner || isOwnSessionController(control, session.session_id);
}

export class Overlays {
  readonly element = document.createElement("div");
  private readonly title = document.createElement("div");
  private readonly body = document.createElement("div");
  private readonly details = document.createElement("details");
  private readonly detailsText = document.createElement("pre");
  private readonly actions = document.createElement("div");
  private readonly startButton = document.createElement("button");
  private readonly reloadButton = document.createElement("button");
  private readonly askAgentButton = document.createElement("button");
  private readonly askFailure = document.createElement("div");
  private readonly askFailureMessage = document.createElement("p");
  private readonly askFailureDetails = document.createElement("details");
  private readonly askFailureDetailsText = document.createElement("pre");
  private readonly fallbackActions = document.createElement("div");
  private readonly draftAskButton = document.createElement("button");
  private readonly reportBugButton = document.createElement("button");
  private readonly dim = document.createElement("div");
  private readonly dimTitle = document.createElement("div");
  private readonly dimPending = document.createElement("div");
  private readonly dimTake = document.createElement("button");
  private readonly dimCancel = document.createElement("button");
  private host: FailureHost | null = null;
  private agentFix: AgentFixRequest | null = null;
  private reported: ReportedFailure | null = null;
  private isHelpOwed = false;
  /** Which failure the overlay shows: changes when it hides or shows another, so an ask settling later knows. */
  private shownFailure = 0;
  /** The failure the pending ask was made about (``shownFailure`` at the press), or null when none is pending. */
  private askingAbout: number | null = null;
  /** Counts presses, so an answer to an earlier press never replaces what a later one shows. */
  private askPresses = 0;
  private askFailureShown: ShownFailure | null = null;

  constructor(onStart: () => void, onTakeControl: () => void, onCancelPendingUrl: () => void, onReload: () => void) {
    this.element.className = "screen-overlay";
    this.element.dataset.screenOverlay = "true";
    this.title.className = "screen-overlay__title";
    this.title.dataset.screenOverlayTitle = "true";
    this.body.className = "screen-overlay__detail";
    this.body.dataset.screenOverlayBody = "true";
    const summary = document.createElement("summary");
    summary.textContent = "See details";
    this.details.className = "screen-overlay__details see-details";
    this.details.dataset.screenOverlayDetails = "true";
    this.details.append(summary, this.detailsText);
    this.actions.className = "screen-overlay__actions";
    this.startButton.className = "screen-button screen-button--primary";
    this.startButton.dataset.screenStart = "true";
    this.startButton.addEventListener("click", () => onStart());
    this.reloadButton.className = "screen-button screen-button--primary";
    this.reloadButton.textContent = "Try again";
    this.reloadButton.dataset.screenReload = "true";
    this.reloadButton.addEventListener("click", () => onReload());
    this.askAgentButton.className = "screen-button ask-agent-to-fix";
    this.askAgentButton.textContent = ASK_AGENT_TO_FIX_LABEL;
    this.askAgentButton.dataset.screenAskAgent = "true";
    this.askAgentButton.addEventListener("click", () => void this.askAgent());
    this.draftAskButton.className = "screen-button draft-ask";
    this.draftAskButton.textContent = DRAFT_ASK_LABEL;
    this.draftAskButton.dataset.screenDraftAsk = "true";
    this.draftAskButton.addEventListener("click", () => {
      if (this.host === null || this.agentFix === null) return;
      // The shell tells a draft that could not be made itself.
      void askAgentToFix(this.host, this.agentFix, { showsOutcome: true }, "draft");
    });
    this.reportBugButton.className = "screen-button report-bug";
    this.reportBugButton.textContent = REPORT_A_BUG_LABEL;
    this.reportBugButton.dataset.screenReportBug = "true";
    this.reportBugButton.addEventListener("click", () => {
      if (this.host !== null && this.reported !== null) {
        this.host.reportBug(withAskFailure(this.reported, this.askFailureShown));
      }
    });
    this.actions.append(this.startButton, this.reloadButton, this.askAgentButton);
    this.fallbackActions.className = "screen-overlay__actions";
    this.fallbackActions.append(this.draftAskButton, this.reportBugButton);
    const askFailureSummary = document.createElement("summary");
    askFailureSummary.textContent = "See details";
    this.askFailureDetails.className = "screen-overlay__details see-details";
    this.askFailureDetails.append(askFailureSummary, this.askFailureDetailsText);
    this.askFailure.className = "screen-overlay__detail";
    this.askFailure.setAttribute("role", "alert");
    this.askFailure.dataset.screenAskFailure = "true";
    this.askFailure.hidden = true;
    this.askFailure.append(this.askFailureMessage, this.askFailureDetails);
    this.element.append(this.title, this.body, this.details, this.actions, this.askFailure, this.fallbackActions);
    // Until the first state arrives, the page is loading: nothing to start, nothing failed.
    this.showLoading();
    this.dim.className = "screen-overlay screen-overlay--dim";
    this.dim.hidden = true;
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

  /** The shell connection the failure buttons go through; its handshake decides which of them show. */
  setHost(host: FailureHost): void {
    this.host = host;
  }

  update(
    state: ScreenStateDocument,
    streamStatus: StreamStatus,
    session: NekoSessionAnswer | null,
    signInFailureDetails: string | null,
    startFailure: FailureMessage | null,
  ): void {
    this.render(describeOverlay(state, streamStatus, session, signInFailureDetails, startFailure));
    const control: ControlDocument = state.control;
    const isAgent = control.controller === "agent" && state.lifecycle === "running";
    this.dim.hidden = !isAgent;
    if (isAgent) this.dimTitle.textContent = `${control.agent_name ?? control.agent_id ?? "An agent"} has control`;
    const pendingUrl = state.chromium.pending_url;
    const hasPending = isAgent && pendingUrl !== null;
    this.dimPending.hidden = !hasPending;
    this.dimCancel.hidden = !(hasPending && mayCancelPendingUrl(control, session));
    if (hasPending) {
      this.dimPending.textContent = `Waiting to open ${pendingUrl} in the browser: take control to open it now`;
      this.dimTake.textContent = "Take control and open it";
    } else {
      this.dimTake.textContent = "Take control";
    }
  }

  /** Show "Ask an agent to fix this" as the host now says it can work, for what the overlay shows, and what stands
   *  in for it: "Draft it in a chat" and "Report a bug" under an ask that was refused in a way that owes help, and
   *  "Report a bug" alone for a failure that owes help where no agent can be asked. The shell's handshake can arrive
   *  after the overlay was drawn. */
  syncShellButtons(): void {
    const isAgentOffered = this.agentFix !== null && this.host !== null && this.host.hasShell();
    const isAskRefused = isAgentOffered && this.askFailureShown?.isHelpOwed === true;
    this.askAgentButton.hidden = !isAgentOffered;
    this.draftAskButton.hidden = !isAskRefused;
    // One Report a bug files the overlay's failure and a refused ask's together.
    const isReportOffered = isAskRefused || (this.isHelpOwed && !isAgentOffered);
    this.reportBugButton.hidden = !isReportOffered || this.host === null || !this.host.canReportBug();
    this.fallbackActions.hidden = this.draftAskButton.hidden && this.reportBugButton.hidden;
  }

  /** "Ask an agent to fix this": pending until the shell says the chat about the Screen opened, or why it could not. */
  private async askAgent(): Promise<void> {
    if (this.host === null || this.agentFix === null || this.askingAbout === this.shownFailure) return;
    const askedAbout = this.shownFailure;
    this.askPresses += 1;
    const press = this.askPresses;
    this.askingAbout = askedAbout;
    this.showAskFailure(null);
    this.syncAskButton();
    // Applies only while the overlay still shows the failure asked about and no press came since.
    const show = (outcome: StartOutcome): void => {
      if (this.shownFailure === askedAbout && this.askPresses === press) {
        this.showAskFailure(outcome.isOpened ? null : outcome.failure);
      }
    };
    const outcome = await askAgentToFix(this.host, this.agentFix, { showsOutcome: true, onLateAnswer: show });
    if (this.askingAbout === askedAbout && this.askPresses === press) this.askingAbout = null;
    this.syncAskButton();
    show(outcome);
  }

  private syncAskButton(): void {
    const isAsking = this.askingAbout === this.shownFailure;
    this.askAgentButton.textContent = isAsking ? ASKING_AN_AGENT_LABEL : ASK_AGENT_TO_FIX_LABEL;
    this.askAgentButton.disabled = isAsking;
  }

  private showAskFailure(failure: ShownFailure | null): void {
    this.askFailureShown = failure;
    this.askFailure.hidden = failure === null;
    this.askFailureMessage.textContent = failure?.message ?? "";
    this.askFailureDetails.hidden = failure?.details == null;
    this.askFailureDetailsText.textContent = failure?.details ?? "";
    this.syncShellButtons();
  }

  /** The page could not load the state at all; the service's events bring it once the service answers. */
  showLoadFailure(failure: Failure): void {
    this.render(describeLoadFailure(failure));
  }

  /** Try again was pressed on a load failure: the page is loading again. */
  showLoading(): void {
    this.render(view("Loading the Screen"));
  }

  private render(shown: OverlayView | null): void {
    // A failed ask belongs to the failure it was asked about.
    if (shown === null || this.element.hidden || shown.title !== this.title.textContent) {
      this.shownFailure += 1;
      this.showAskFailure(null);
      this.syncAskButton();
    }
    if (shown === null) {
      this.element.hidden = true;
      return;
    }
    this.element.hidden = false;
    this.title.textContent = shown.title;
    this.body.textContent = shown.body;
    this.body.hidden = shown.body === "";
    this.details.hidden = shown.details === null;
    this.detailsText.textContent = shown.details ?? "";
    this.startButton.hidden = shown.startLabel === null;
    this.startButton.textContent = shown.startLabel ?? "";
    this.reloadButton.hidden = !shown.isReloadOffered;
    this.agentFix = shown.agentFix;
    this.reported = {
      message: shown.body === "" ? shown.title : `${shown.title} ${shown.body}`,
      details: shown.details,
    };
    this.isHelpOwed = shown.isHelpOwed;
    this.syncShellButtons();
  }
}
