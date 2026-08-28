/**
 * Modal that walks the user through signing Claude in inside a mind. It is
 * the sole auth surface for a workspace. Five sign-in paths:
 *
 * - Claude subscription (primary): drive `claude auth login --claudeai`
 *   via the backend's PTY subprocess. The CLI stores a credential that
 *   running claudes re-read on their next API call, so a fresh workspace
 *   signs in with NO agent restart; when managed settings-env keys are
 *   active they are cleared and the agents restarted (the switching case).
 * - Sign in with Imbue: ask the embedding minds chrome (via the embed
 *   contract's `minds:open-ai-keys-page` message, keyed by this workspace's id
 *   host id) to open its key-mint page over this window, then paste the
 *   copied env-style blob into a textarea. Any minds chrome -- Electron or
 *   plain-browser -- acks the message; with no ack (a direct share visit) an
 *   alert explains that the minds app is required.
 * - Raw API key: paste a `sk-ant-...` value (wrapped into an env-style
 *   line client-side).
 * - Get a long-lived token: `claude setup-token` mints a 1-year token
 *   written into the settings env block (restarts the agents).
 * - Anthropic Console (API billing): `claude auth login --console`; its
 *   key lives in `.claude.json`, so it always restarts the agents.
 *
 * The paste paths (Imbue blob, API key, subtle direct-token affordance)
 * hit one strict backend endpoint (`/api/claude-auth/submit-credentials`),
 * which writes the settings env block and restarts the mind's claude
 * agents; restarts run in the background and are rendered as a step
 * checklist driven by the status endpoint's restart_* fields.
 *
 * The modal is a single app-level instance: global auth state
 * (models/ClaudeAuth.ts) opens it on load-time status failure, on any
 * transcript auth-error, and from the persistent "Agent auth" entry in the
 * chat footer. A muted header line shows how the mind is currently signed
 * in (derived server-side from the settings env content, folding in
 * `claude auth status` for the credentials-based browser sign-ins).
 */

import m from "mithril";
import { backdropDismissAttrs } from "./components/modalBackdrop";
import { OPEN_AI_KEYS_ACK, OPEN_AI_KEYS_PAGE } from "@minds/embed-contract";
import { apiUrl } from "../base-path";
import { clearEmbedderMessageHandler, sendToEmbedder, setEmbedderMessageHandler } from "../embed";
import { claudeLogoIcon, icon, loginSpinnerIcon, warningIcon } from "./components/icons";
import { Button, buttonClass } from "./components/Button";
import { inputClass } from "./components/Input";

interface ClaudeAuthStatus {
  logged_in: boolean;
  auth_method?: string | null;
  api_provider?: string | null;
  email?: string | null;
  org_id?: string | null;
  org_name?: string | null;
  subscription_type?: string | null;
  auth_mode?: string;
  masked_key_suffix?: string | null;
  workspace_id?: string | null;
  restart_phase?: string | null;
  restart_detail?: string | null;
  restart_error?: string | null;
  restart_reason?: string | null;
}

// Which PTY-driven browser flow the awaiting screen is running: the primary
// subscription sign-in, the Console sign-in, or the long-lived-token minting.
type AuthFlow = "claudeai" | "console" | "setup_token";

interface SetupTokenStartResponse {
  session_id: string;
  oauth_url: string;
}

interface SetupTokenPollResponse {
  is_complete: boolean;
  status?: ClaudeAuthStatus | null;
}

type Mode =
  | "select_provider"
  | "api_key_form"
  | "imbue_form"
  | "awaiting_setup_token"
  | "verifying"
  | "applying"
  | "success"
  | "error";

// How often the modal asks the backend whether `claude setup-token` has
// minted the token yet while the awaiting screen is up.
const SETUP_TOKEN_POLL_INTERVAL_MS = 2000;

// How often the modal refreshes the status endpoint while the background
// agent restart runs (the "applying" checklist screen).
const APPLYING_POLL_INTERVAL_MS = 1000;

export interface ClaudeLoginModalAttrs {
  // Called when the user closes the modal -- either after a successful
  // sign-in flow ("Done" button) or via the close affordance before
  // signing in. A subsequent auth-error event will reopen it.
  onDismiss: () => void;
}

// How long to wait for the desktop shell's relay to acknowledge an
// open-the-Imbue-key-page request (see openImbueMintPage). The relay acks
// immediately on receipt, so anything beyond a few event-loop turns means no
// relay is listening -- this page is not being viewed inside the desktop app.
const MINT_PAGE_ACK_TIMEOUT_MS = 300;

/* Styling.
 * Utilities in the markup; the claude-login-* class names stay as bare markers
 * (the vitest suite drives the flow by them). The modal deliberately stays off
 * the shared .modal-* shell: it is a scrollable, sectioned, multi-step flow
 * whose header/body/footer own their padding, and it sits at z-50 -- a
 * design-system-exception mid layer below the main modal overlay stack. Its
 * two entry animations stay in style.css as keyframes. */

const OVERLAY_CLASS =
  "claude-login-overlay absolute inset-0 z-50 flex items-center justify-center bg-[rgba(20,20,20,0.45)] p-4 " +
  "backdrop-blur-[3px] animate-[claude-login-overlay-in_150ms_ease-out]";

const MODAL_CLASS =
  "claude-login-modal relative flex max-h-[calc(100vh-32px)] w-full max-w-[460px] flex-col overflow-hidden " +
  "rounded-lg bg-surface shadow-overlay animate-[claude-login-modal-in_var(--dur-slow)_cubic-bezier(0.16,1,0.3,1)]";

/** The one-line intro above a form's inputs. */
const LEAD_CLASS = "claude-login-lead mb-4.5 text-(length:--font-size-body) leading-normal text-secondary";

/** A numbered OAuth-flow step and its label/badge. */
const STEP_CLASS = "claude-login-step mb-4 last:mb-0";
const STEP_LABEL_CLASS =
  "claude-login-step-label mb-2 flex items-center gap-2 text-(length:--font-size-body) font-semibold text-primary";
const STEP_NUM_CLASS =
  "claude-login-step-num inline-flex h-[18px] w-[18px] items-center justify-center rounded-full bg-accent " +
  "text-(length:--font-size-helper) font-semibold text-on-accent";

const HELPER_CLASS = "claude-login-helper mt-1.5 text-(length:--font-size-helper) leading-[1.4] text-secondary";

/** The provider cards behind the "Other ways to sign in" disclosure. `group`
 *  carries the card hover to the trailing chevron. The hover shadow is a
 *  design-system-exception: a tighter 6px-blur lift of the raised card,
 *  between scale tiers. */
const ALT_CLASS =
  "claude-login-alt group flex w-full cursor-pointer items-center justify-between gap-3 rounded-lg border " +
  "bg-surface px-3.5 py-3 text-left shadow-raised transition-[border-color,box-shadow] duration-100 ease-[ease] " +
  "hover:border-accent hover:shadow-[0_2px_6px_rgba(0,0,0,0.08)] " +
  "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent";

/** The "Didn't open? Copy the link" fallback line and its inline text action.
 *  focus-visible:rounded-[2px]: design-system-exception -- a tight one-off
 *  focus-ring radius on an inline text link, between scale steps. */
const COPYLINK_CLASS = "claude-login-copylink mt-[9px] text-(length:--font-size-helper) leading-normal text-secondary";
const COPYLINK_ACTION_CLASS =
  "claude-login-copylink-action cursor-pointer border-none bg-transparent p-0 " +
  "text-(length:--font-size-helper) font-semibold text-accent underline underline-offset-2 " +
  "hover:text-accent-hover focus-visible:rounded-[2px] focus-visible:outline-2 focus-visible:outline-offset-2 " +
  "focus-visible:outline-accent";

/** An input row (paste-code, direct-token, and the collapsed affordances). */
const SUBTLE_BODY_CLASS = "claude-login-subtle-body mt-1.5 flex items-center gap-2";

const FOOTER_BASE = "claude-login-footer flex items-center gap-2 border-t border-subtle px-5.5 pt-3.5 pb-4.5";

export function ClaudeLoginModal(): m.Component<ClaudeLoginModalAttrs> {
  let mode: Mode = "select_provider";
  let activeFlow: AuthFlow = "claudeai";
  let sessionId: string | null = null;
  let oauthUrl: string | null = null;
  let code = "";
  let apiKey = "";
  let apiKeyRevealed = false;
  let imbueBlob = "";
  let directToken = "";
  // Whether the direct-token affordance on the awaiting screen is
  // expanded. Collapsed by default: it is a developer shortcut, unlike the
  // always-visible paste-code input.
  let tokenPasteExpanded = false;
  let urlCopied = false;
  // Set when a clipboard write was attempted but rejected (insecure context,
  // denied permission). Drives the "Failed to copy" label and the raw-URL
  // fallback block so the user can still select and copy the link by hand.
  let urlCopyFailed = false;
  let urlCopiedResetHandle: ReturnType<typeof setTimeout> | null = null;
  // Pending ack handshake for the "Open the Imbue key page" embed-contract
  // request (see openImbueMintPage). Cleared on ack, timeout, or modal
  // teardown. True while the ack handler is registered with the embed
  // endpoint.
  let mintAckTimer: ReturnType<typeof setTimeout> | null = null;
  let isMintAckHandlerRegistered = false;
  let pollHandle: ReturnType<typeof setInterval> | null = null;
  let pollInFlight = false;
  // Status polling for the "applying" screen: the background agent restart
  // reports its progress through the status endpoint's restart_* fields.
  let applyingPollHandle: ReturnType<typeof setInterval> | null = null;
  let applyingPollInFlight = false;
  let applyingStatus: ClaudeAuthStatus | null = null;
  let errorMessage: string | null = null;
  let verifyingTitle = "Working...";
  let verifyingDetail: string | null = null;
  let successStatus: ClaudeAuthStatus | null = null;
  // True when the flow now underway started while the workspace was signed in
  // with an API key (raw or Imbue). Switching away leaves any API-integrated
  // services pinned to the key they were set up with (they snapshot it to
  // data/.secrets/anthropic.env; see the use-ai-integration skill), so the
  // applying/success screens carry a note saying so. Captured at flow start:
  // by the time those screens render, the live status may already reflect the
  // new sign-in.
  let switchedAwayFromApiKey = false;
  // Fetched when the modal opens; drives the muted "currently signed in
  // via ..." header on the provider-selection screen and the Imbue
  // mint-page link (which needs the workspace host id).
  let currentStatus: ClaudeAuthStatus | null = null;
  let attrsRef: ClaudeLoginModalAttrs | null = null;
  // Whether the "Other ways to sign in" section on the provider-selection
  // screen is expanded. Collapsed by default so the Claude subscription
  // path -- the option most users want -- carries the visual weight.
  let alternativesExpanded = false;

  function clearError(): void {
    errorMessage = null;
  }

  function setError(message: string): void {
    stopPolling();
    stopApplyingPoll();
    errorMessage = message;
    mode = "error";
    m.redraw();
  }

  // Surface a failure inline within a form, where the user can simply
  // re-submit in place, instead of swapping to the full-screen `error`
  // view. Setup-token failures do NOT use this: a failed session is
  // consumed backend-side, so they route to `setError` (the full "Start
  // over" screen).
  function setInlineError(message: string, formMode: "api_key_form" | "imbue_form" | "awaiting_setup_token"): void {
    errorMessage = message;
    mode = formMode;
    m.redraw();
  }

  function startVerifying(title: string, detail: string | null): void {
    stopPolling();
    verifyingTitle = title;
    verifyingDetail = detail;
    mode = "verifying";
    m.redraw();
  }

  function loadCurrentStatus(): void {
    // The header line is progressive enhancement; any failure (including a
    // synchronous one from environments without a DOM, e.g. unit tests)
    // just leaves it blank.
    let statusRequest: Promise<ClaudeAuthStatus>;
    try {
      statusRequest = m.request<ClaudeAuthStatus>({ method: "GET", url: apiUrl("/api/claude-auth/status") });
    } catch {
      currentStatus = null;
      return;
    }
    void statusRequest
      .then((status) => {
        currentStatus = status;
        m.redraw();
      })
      .catch(() => {
        currentStatus = null;
      });
  }

  function stopPolling(): void {
    if (pollHandle !== null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
    pollInFlight = false;
  }

  function startPolling(): void {
    stopPolling();
    pollHandle = setInterval(() => {
      void pollSetupToken();
    }, SETUP_TOKEN_POLL_INTERVAL_MS);
  }

  function stopApplyingPoll(): void {
    if (applyingPollHandle !== null) {
      clearInterval(applyingPollHandle);
      applyingPollHandle = null;
    }
    applyingPollInFlight = false;
  }

  // Route a successful credential submit: the backend has written the
  // settings env and kicked the agent restart onto a background thread, so
  // the returned status carries the restart's initial progress. Show the
  // step checklist and follow the restart via the status endpoint until it
  // reports done (success screen) or failed (error screen).
  function enterApplyingOrSuccess(status: ClaudeAuthStatus): void {
    if (status.restart_phase === "failed") {
      setError(status.restart_error ?? "Restarting the agents failed.");
      return;
    }
    if (status.restart_phase === "done" || status.restart_phase == null) {
      successStatus = status;
      mode = "success";
      m.redraw();
      return;
    }
    applyingStatus = status;
    mode = "applying";
    stopApplyingPoll();
    applyingPollHandle = setInterval(() => {
      void pollApplying();
    }, APPLYING_POLL_INTERVAL_MS);
    m.redraw();
  }

  async function pollApplying(): Promise<void> {
    if (mode !== "applying" || applyingPollInFlight) return;
    applyingPollInFlight = true;
    try {
      const status = await m.request<ClaudeAuthStatus>({ method: "GET", url: apiUrl("/api/claude-auth/status") });
      applyingStatus = status;
      if (status.restart_phase === "done") {
        stopApplyingPoll();
        successStatus = status;
        mode = "success";
      } else if (status.restart_phase === "failed") {
        stopApplyingPoll();
        setError(status.restart_error ?? "Restarting the agents failed.");
        return;
      }
      m.redraw();
    } catch {
      // A transient status failure just means this tick learns nothing;
      // keep polling -- the restart continues server-side regardless.
    } finally {
      applyingPollInFlight = false;
    }
  }

  // Endpoint families for the two PTY session kinds: the browser sign-ins
  // (claudeai / console) share the oauth endpoints; the long-lived-token
  // flow keeps its setup-token endpoints.
  function flowBaseUrl(): string {
    return activeFlow === "setup_token" ? "/api/claude-auth/setup-token" : "/api/claude-auth/oauth";
  }

  async function startSetupToken(): Promise<void> {
    activeFlow = "setup_token";
    switchedAwayFromApiKey = currentStatus?.auth_mode === "api_key" || currentStatus?.auth_mode === "imbue";
    await startAuthFlowSession("/api/claude-auth/setup-token/start", undefined);
  }

  async function startOauthLogin(provider: "claudeai" | "console"): Promise<void> {
    activeFlow = provider;
    switchedAwayFromApiKey = currentStatus?.auth_mode === "api_key" || currentStatus?.auth_mode === "imbue";
    await startAuthFlowSession("/api/claude-auth/oauth/start", { provider });
  }

  async function startAuthFlowSession(path: string, body: object | undefined): Promise<void> {
    clearError();
    startVerifying("Starting sign-in...", "Preparing your sign-in.");
    try {
      // apiUrl is resolved inside the try: it can throw synchronously in a
      // DOM-less environment, and any failure must land on the error screen.
      const response = await m.request<SetupTokenStartResponse>({
        method: "POST",
        url: apiUrl(path),
        body,
      });
      sessionId = response.session_id;
      oauthUrl = response.oauth_url;
      tokenPasteExpanded = false;
      mode = "awaiting_setup_token";
      startPolling();
      m.redraw();
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setError(errResp?.detail ?? "Failed to start the sign-in");
    }
  }

  async function pollSetupToken(): Promise<void> {
    if (sessionId === null || pollInFlight || mode !== "awaiting_setup_token") return;
    pollInFlight = true;
    const polledSessionId = sessionId;
    try {
      const response = await m.request<SetupTokenPollResponse>({
        method: "POST",
        url: apiUrl(`${flowBaseUrl()}/poll`),
        body: { session_id: polledSessionId },
      });
      if (response.is_complete && response.status) {
        stopPolling();
        sessionId = null;
        if (response.status.logged_in) {
          enterApplyingOrSuccess(response.status);
        } else {
          setError("Sign-in completed but Claude still reports it is not authenticated.");
          return;
        }
      }
    } catch (error) {
      // A poll error means the backend session is gone (crashed subprocess,
      // replaced session). There is nothing to retry against in place.
      stopPolling();
      sessionId = null;
      const errResp = (error as { response?: { detail?: string } }).response;
      setError(errResp?.detail ?? "The sign-in session was interrupted");
    } finally {
      pollInFlight = false;
    }
  }

  async function submitSetupTokenCode(): Promise<void> {
    if (!sessionId || !code.trim()) return;
    clearError();
    startVerifying("Verifying code...", "Completing sign-in.");
    const submittedSessionId = sessionId;
    // The backend clears its in-flight session record once the code is
    // sent, so the id we just submitted is consumed regardless of whether
    // auth succeeded. Clear it locally too so a later modal-unmount does
    // not fire a spurious /abort against a discarded session.
    sessionId = null;
    try {
      const status = await m.request<ClaudeAuthStatus>({
        method: "POST",
        url: apiUrl(`${flowBaseUrl()}/submit-code`),
        body: {
          session_id: submittedSessionId,
          code: code.trim(),
        },
      });
      if (status.logged_in) {
        enterApplyingOrSuccess(status);
      } else {
        // A submitted code consumes the single-use session, so there is
        // nothing left to retry in place. Route to the full error screen,
        // whose only action is "Start over" (a fresh sign-in flow).
        setError("Authentication did not succeed.");
      }
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      // Same single-use-session reasoning as the branch above.
      setError(errResp?.detail ?? "Failed to verify code");
    }
  }

  // All three paste paths (API key, Imbue blob, direct token) submit
  // env-var-style lines to the same strict backend endpoint, which writes
  // the settings env block and restarts the mind's claude agents.
  async function submitCredentialLines(
    credentialLines: string,
    verifyingCopy: string,
    failureFormMode: "api_key_form" | "imbue_form" | "awaiting_setup_token",
  ): Promise<void> {
    clearError();
    startVerifying(verifyingCopy, "Applying to this mind and restarting its agents.");
    try {
      const status = await m.request<ClaudeAuthStatus>({
        method: "POST",
        url: apiUrl("/api/claude-auth/submit-credentials"),
        body: {
          credentials: credentialLines,
        },
      });
      if (status.logged_in) {
        enterApplyingOrSuccess(status);
      } else {
        setInlineError("Claude did not accept the credentials. Double-check and try again.", failureFormMode);
      }
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setInlineError(errResp?.detail ?? "Failed to save credentials", failureFormMode);
    }
  }

  function submitApiKey(): void {
    if (!apiKey.trim()) return;
    void submitCredentialLines(`ANTHROPIC_API_KEY=${apiKey.trim()}`, "Saving your API key...", "api_key_form");
  }

  function submitImbueBlob(): void {
    if (!imbueBlob.trim()) return;
    void submitCredentialLines(imbueBlob, "Saving your Imbue credentials...", "imbue_form");
  }

  function submitDirectToken(): void {
    if (!directToken.trim()) return;
    // The direct-token paste replaces the in-flight setup-token session,
    // so drop that session first.
    abortSetupTokenIfActive();
    void submitCredentialLines(
      `CLAUDE_CODE_OAUTH_TOKEN=${directToken.trim()}`,
      "Saving your token...",
      "awaiting_setup_token",
    );
  }

  function abortSetupTokenIfActive(): void {
    stopPolling();
    if (sessionId !== null) {
      void m.request({ method: "POST", url: apiUrl("/api/claude-auth/abort") });
    }
    sessionId = null;
    oauthUrl = null;
    code = "";
    resetUrlCopied();
  }

  function resetUrlCopied(): void {
    urlCopied = false;
    urlCopyFailed = false;
    if (urlCopiedResetHandle !== null) {
      clearTimeout(urlCopiedResetHandle);
      urlCopiedResetHandle = null;
    }
  }

  async function copyOAuthUrl(): Promise<void> {
    if (!oauthUrl) return;
    try {
      await navigator.clipboard.writeText(oauthUrl);
    } catch {
      // Clipboard access can be denied (insecure context, permissions).
      // Tell the user the copy failed and reveal the raw URL below so they
      // can select and copy it manually. Clear any stale "Link copied"
      // state from a recent successful copy so the UI isn't contradictory.
      urlCopied = false;
      if (urlCopiedResetHandle !== null) {
        clearTimeout(urlCopiedResetHandle);
        urlCopiedResetHandle = null;
      }
      urlCopyFailed = true;
      m.redraw();
      return;
    }
    urlCopyFailed = false;
    urlCopied = true;
    if (urlCopiedResetHandle !== null) clearTimeout(urlCopiedResetHandle);
    urlCopiedResetHandle = setTimeout(() => {
      urlCopied = false;
      urlCopiedResetHandle = null;
      m.redraw();
    }, 2000);
    m.redraw();
  }

  function goBackToProviderSelection(): void {
    abortSetupTokenIfActive();
    stopApplyingPoll();
    apiKey = "";
    apiKeyRevealed = false;
    imbueBlob = "";
    directToken = "";
    switchedAwayFromApiKey = false;
    clearError();
    loadCurrentStatus();
    mode = "select_provider";
    m.redraw();
  }

  // Tear down a pending open-the-Imbue-key-page handshake (ack listener +
  // fallback timer). Called on ack, on timeout, on a re-click, and on modal
  // teardown so a stale timer can never fire the alert later.
  function clearMintAckWait(): void {
    if (mintAckTimer !== null) {
      clearTimeout(mintAckTimer);
      mintAckTimer = null;
    }
    if (isMintAckHandlerRegistered) {
      clearEmbedderMessageHandler(OPEN_AI_KEYS_ACK);
      isMintAckHandlerRegistered = false;
    }
  }

  function openImbueMintPage(): void {
    // The mint page is served by the minds app, whose origin this workspace
    // page cannot know (the app's backend listens on a random per-run port).
    // Ask the embedding minds chrome to open it over this window via the
    // embed contract; the chrome acks immediately, so a missing ack means
    // this page is not being viewed under a minds chrome (a direct share
    // visit) and the mint page is unreachable from this browser.
    clearMintAckWait();
    setEmbedderMessageHandler(OPEN_AI_KEYS_ACK, clearMintAckWait);
    isMintAckHandlerRegistered = true;
    mintAckTimer = setTimeout(() => {
      clearMintAckWait();
      window.alert(
        "The Imbue key page is part of the Minds app. Open this workspace from the Minds app on your computer to mint a key, then paste it here.",
      );
    }, MINT_PAGE_ACK_TIMEOUT_MS);
    // The embed-contract field stays named hostId for wire compatibility;
    // the value is the workspace id (older minds chromes dual-accept both).
    sendToEmbedder(OPEN_AI_KEYS_PAGE, { hostId: currentStatus?.workspace_id ?? "" });
  }

  // ----- Renderers -----

  function describeCurrentMode(status: ClaudeAuthStatus | null): string | null {
    if (status === null) return null;
    const suffix = status.masked_key_suffix ? ` (...${status.masked_key_suffix})` : "";
    if (status.auth_mode === "subscription") {
      return status.email
        ? `Currently signed in with your Claude subscription (${status.email}).`
        : "Currently signed in with your Claude subscription.";
    }
    if (status.auth_mode === "console") return "Currently signed in with your Anthropic Console account.";
    if (status.auth_mode === "imbue") return `Currently signed in with Imbue${suffix}.`;
    if (status.auth_mode === "api_key") return `Currently signed in with an API key${suffix}.`;
    if (status.logged_in) return "Currently signed in.";
    return "Not signed in.";
  }

  // The provider-selection screen leads with the Claude subscription as the
  // recommended default -- a logo, headline, and full-width primary button --
  // and tucks the Imbue and API-key paths behind a collapsed "Other ways to
  // sign in" disclosure so they don't compete for attention.
  function renderAltCard(name: string, desc: string, onclick: () => void): m.Vnode {
    return m("button", { class: ALT_CLASS, type: "button", onclick }, [
      m("span", { class: "claude-login-alt-text flex min-w-0 flex-col gap-[3px]" }, [
        m("span", { class: "claude-login-alt-name text-(length:--font-size-body) font-semibold text-primary" }, name),
        m(
          "span",
          { class: "claude-login-alt-desc text-(length:--font-size-helper) leading-[1.4] text-secondary" },
          desc,
        ),
      ]),
      m(
        "span",
        {
          class:
            "claude-login-alt-go flex flex-none text-faint transition-colors duration-100 ease-[ease] group-hover:text-accent",
        },
        m.trust(icon("chevron-right", { size: 18 })),
      ),
    ]);
  }

  function renderProviderSelection(): m.Vnode {
    const currentModeLine = describeCurrentMode(currentStatus);
    return m("div", { class: "claude-login-select mb-2" }, [
      currentModeLine !== null
        ? m(
            "p",
            { class: "claude-login-current-mode mb-2.5 text-center text-(length:--font-size-helper) text-faint" },
            currentModeLine,
          )
        : null,
      m("div", { class: "claude-login-primary flex flex-col items-center px-1 pt-1 pb-0.5 text-center" }, [
        m.trust(claudeLogoIcon()),
        m(
          "h3",
          {
            class:
              "claude-login-primary-headline mb-1 text-(length:--font-size-heading) font-bold tracking-[-0.005em] text-primary",
          },
          "Sign in with your Claude subscription",
        ),
        m(
          "p",
          {
            class:
              "claude-login-primary-sub mx-auto mb-4 max-w-[320px] text-(length:--font-size-body) leading-normal text-secondary",
          },
          "Connect your Claude.ai account to use your Pro or Max plan quota in this mind.",
        ),
        m(
          Button,
          {
            variant: "primary",
            block: true,
            onclick: () => void startOauthLogin("claudeai"),
          },
          "Continue with Claude subscription",
        ),
      ]),
      m("div", { class: "claude-login-alts mt-3.5 border-t border-subtle pt-3" }, [
        m(
          "button",
          {
            class:
              "claude-login-alts-toggle mx-auto flex cursor-pointer items-center justify-center gap-1.5 rounded-md " +
              "border-none bg-transparent px-2 py-1 text-(length:--font-size-body) font-semibold text-secondary " +
              "hover:text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
            type: "button",
            "aria-expanded": String(alternativesExpanded),
            onclick: () => {
              alternativesExpanded = !alternativesExpanded;
              m.redraw();
            },
          },
          [
            m("span", "Other ways to sign in"),
            m(
              "span",
              {
                class:
                  "claude-login-alts-caret inline-flex transition-transform duration-(--dur-base) ease-[ease] " +
                  (alternativesExpanded ? "claude-login-alts-caret--open rotate-180" : ""),
              },
              m.trust(icon("chevron-down", { size: 14 })),
            ),
          ],
        ),
        alternativesExpanded
          ? m("div", { class: "claude-login-alts-list mt-2.5 flex flex-col gap-2" }, [
              renderAltCard(
                "Sign in with Imbue",
                "Use an Imbue account to pay per token, no Claude account needed.",
                () => {
                  mode = "imbue_form";
                  m.redraw();
                },
              ),
              renderAltCard("Use an API key", "Paste a raw sk-ant-... API key.", () => {
                mode = "api_key_form";
                m.redraw();
              }),
              renderAltCard(
                "Get a long-lived token",
                "Mint a 1-year subscription token (restarts this mind's agents).",
                () => void startSetupToken(),
              ),
              renderAltCard(
                "Anthropic Console (API billing)",
                "Sign in with a Console account to pay per token (restarts this mind's agents).",
                () => void startOauthLogin("console"),
              ),
            ])
          : null,
      ]),
    ]);
  }

  function renderApiKeyForm(): m.Vnode[] {
    return [
      m("p", { class: LEAD_CLASS }, "Paste an Anthropic API key. It's saved to this mind's shared Claude settings."),
      m("div", { class: "claude-login-field flex flex-col" }, [
        m("label", { class: STEP_LABEL_CLASS, for: "claude-login-api-key-input" }, [
          m("span", { class: STEP_NUM_CLASS }, "1"),
          "Your Anthropic API key",
        ]),
        m("div", { class: "claude-login-input-wrap relative flex" }, [
          m("input", {
            class: inputClass({ mono: true, withAction: true }),
            id: "claude-login-api-key-input",
            type: apiKeyRevealed ? "text" : "password",
            placeholder: "sk-ant-...",
            value: apiKey,
            spellcheck: false,
            autocomplete: "off",
            oninput: (event: InputEvent) => {
              apiKey = (event.target as HTMLInputElement).value;
            },
            onkeydown: (event: KeyboardEvent) => {
              if (event.key === "Enter" && apiKey.trim()) {
                event.preventDefault();
                submitApiKey();
              }
            },
          }),
          m(
            "button",
            {
              class:
                "claude-login-input-action absolute top-1/2 right-1.5 -translate-y-1/2 cursor-pointer rounded-sm " +
                "border-none bg-transparent px-2 py-1 text-(length:--font-size-helper) text-secondary " +
                "hover:bg-fill-hover hover:text-primary",
              type: "button",
              onclick: () => {
                apiKeyRevealed = !apiKeyRevealed;
                m.redraw();
              },
              "aria-label": apiKeyRevealed ? "Hide API key" : "Show API key",
            },
            apiKeyRevealed ? "Hide" : "Show",
          ),
        ]),
        m("p", { class: HELPER_CLASS }, "You can find or create API keys at console.anthropic.com."),
      ]),
    ];
  }

  function renderImbueForm(): m.Vnode[] {
    return [
      m(
        "p",
        { class: LEAD_CLASS },
        "Get credentials from the Minds desktop app, then paste them here. Your usage is billed to your Imbue account.",
      ),
      m("div", { class: STEP_CLASS }, [
        m("div", { class: STEP_LABEL_CLASS }, [m("span", { class: STEP_NUM_CLASS }, "1"), "Get your credentials"]),
        // A button, not a link: opening the mint page is a message to the
        // embedding minds chrome (see openImbueMintPage), not a navigation --
        // there is no URL a real anchor could carry.
        m(
          Button,
          {
            variant: "primary",
            block: true,
            onclick: () => openImbueMintPage(),
          },
          [m("span", "Open the Imbue key page"), m.trust(icon("external-link", { size: 15 }))],
        ),
        m(
          "p",
          { class: HELPER_CLASS },
          "The key page creates a key for this workspace and copies the credentials to your clipboard.",
        ),
      ]),
      m("div", { class: STEP_CLASS }, [
        m("label", { class: STEP_LABEL_CLASS, for: "claude-login-imbue-blob-input" }, [
          m("span", { class: STEP_NUM_CLASS }, "2"),
          "Paste your credentials",
        ]),
        m("textarea", {
          class: inputClass({ mono: true, extra: "claude-login-textarea min-h-16 resize-y leading-normal" }),
          id: "claude-login-imbue-blob-input",
          rows: 3,
          placeholder: "ANTHROPIC_BASE_URL=...\nANTHROPIC_API_KEY=sk-...",
          value: imbueBlob,
          spellcheck: false,
          autocomplete: "off",
          oninput: (event: InputEvent) => {
            imbueBlob = (event.target as HTMLTextAreaElement).value;
          },
        }),
      ]),
    ];
  }

  function renderAwaitingSetupToken(): Array<m.Vnode | null> {
    return [
      m("p", { class: LEAD_CLASS }, "Approve access in your browser, then paste the code it shows you."),
      m("div", { class: STEP_CLASS }, [
        m("div", { class: STEP_LABEL_CLASS }, [m("span", { class: STEP_NUM_CLASS }, "1"), "Open the sign-in page"]),
        m(
          "a",
          {
            class: buttonClass("primary", { block: true, extra: "no-underline" }),
            href: oauthUrl,
            target: "_blank",
            rel: "noopener noreferrer",
          },
          [m("span", "Open sign-in page"), m.trust(icon("external-link", { size: 15 }))],
        ),
        m("p", { class: COPYLINK_CLASS }, [
          "Didn't open? ",
          m(
            "button",
            {
              class: COPYLINK_ACTION_CLASS,
              type: "button",
              onclick: () => {
                void copyOAuthUrl();
              },
            },
            urlCopied ? "Link copied" : urlCopyFailed ? "Failed to copy" : "Copy the link",
          ),
          urlCopied ? "" : urlCopyFailed ? " — copy this link manually:" : " and paste it into your browser.",
        ]),
        // Known Anthropic-side sign-in bug (their login page, not this flow);
        // surface the workaround so a hit doesn't dead-end the user.
        m("p", { class: COPYLINK_CLASS }, "malformed_certificate? Try switching accounts or logging out first"),
        // When the clipboard write was rejected, surface the raw URL so the
        // user is never stranded without a way to reach the sign-in page.
        urlCopyFailed && oauthUrl !== null
          ? m(
              "div",
              {
                class:
                  "claude-login-rawurl mt-2 rounded-md border bg-surface px-2.5 py-2 font-mono " +
                  "text-(length:--font-size-helper) leading-[1.4] break-all text-primary select-all " +
                  "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent",
                tabindex: 0,
              },
              oauthUrl,
            )
          : null,
      ]),
      // The approval page shows a CODE#STATE string -- pasting it here is
      // the primary way to finish, so the input is always visible. (The
      // background poll still runs silently: if the CLI's own polling ever
      // completes the flow first, the modal just finishes early.)
      m("div", { class: STEP_CLASS }, [
        m("div", { class: STEP_LABEL_CLASS }, [
          m("span", { class: STEP_NUM_CLASS }, "2"),
          "Approve, then paste the code shown",
        ]),
        m("div", { class: SUBTLE_BODY_CLASS }, [
          m("input", {
            class: inputClass({ mono: true, extra: "flex-1" }),
            id: "claude-login-code-input",
            type: "text",
            placeholder: "CODE#STATE",
            value: code,
            spellcheck: false,
            autocomplete: "off",
            oninput: (event: InputEvent) => {
              code = (event.target as HTMLInputElement).value;
            },
            onkeydown: (event: KeyboardEvent) => {
              if (event.key === "Enter" && code.trim()) {
                event.preventDefault();
                void submitSetupTokenCode();
              }
            },
          }),
          m(
            Button,
            {
              variant: "primary",
              disabled: !code.trim(),
              onclick: () => {
                void submitSetupTokenCode();
              },
            },
            "Verify code",
          ),
        ]),
      ]),
      // Subtle direct-token affordance: developers who already have a
      // long-lived token can skip the browser flow entirely. Only offered
      // on the token-minting flow -- it writes the settings env, which
      // would be misleading on the credentials-based sign-ins.
      activeFlow !== "setup_token"
        ? null
        : m("div", { class: "claude-login-subtle mt-2.5" }, [
            tokenPasteExpanded
              ? m("div", { class: SUBTLE_BODY_CLASS }, [
                  m("input", {
                    class: inputClass({ mono: true, extra: "flex-1" }),
                    id: "claude-login-token-input",
                    type: "password",
                    placeholder: "sk-ant-oat01-...",
                    value: directToken,
                    spellcheck: false,
                    autocomplete: "off",
                    oninput: (event: InputEvent) => {
                      directToken = (event.target as HTMLInputElement).value;
                    },
                    onkeydown: (event: KeyboardEvent) => {
                      if (event.key === "Enter" && directToken.trim()) {
                        event.preventDefault();
                        submitDirectToken();
                      }
                    },
                  }),
                  m(
                    Button,
                    {
                      variant: "primary",
                      disabled: !directToken.trim(),
                      onclick: () => {
                        submitDirectToken();
                      },
                    },
                    "Use token",
                  ),
                ])
              : m(
                  "button",
                  {
                    class:
                      "claude-login-subtle-toggle cursor-pointer border-none bg-transparent p-0 " +
                      "text-(length:--font-size-helper) text-faint hover:text-secondary hover:underline",
                    type: "button",
                    onclick: () => {
                      tokenPasteExpanded = true;
                      m.redraw();
                    },
                  },
                  "Already have a token? Paste it instead",
                ),
          ]),
    ];
  }

  // The step checklist shown while the background credential apply runs,
  // tracking the status endpoint's restart_phase. The leading (already
  // completed) steps depend on why the restart is happening: a plain
  // credential save, or a switch away from managed keys after a browser
  // sign-in (subscription or Console).
  function renderApplying(): m.Vnode {
    const phase = applyingStatus?.restart_phase ?? "restarting";
    const detail = applyingStatus?.restart_detail ?? null;
    const reason = applyingStatus?.restart_reason ?? "credentials_saved";
    const leadLabels =
      reason === "subscription_switch"
        ? ["Signed in with your subscription", "Removing old credentials"]
        : reason === "console_switch"
          ? ["Signed in with your Anthropic Console account", "Removing old credentials"]
          : ["Credentials saved"];
    const phaseOrder = ["restarting", "finishing"];
    const activeIdx = Math.max(0, phaseOrder.indexOf(phase));
    const tailLabels = ["Restarting agents", "Resuming your agent"];
    const steps: { label: string; state: "done" | "active" | "pending" }[] = [
      ...leadLabels.map((label) => ({ label, state: "done" as const })),
      ...tailLabels.map((label, idx) => ({
        label,
        state: idx < activeIdx ? ("done" as const) : idx === activeIdx ? ("active" as const) : ("pending" as const),
      })),
    ];
    // The item's text tone and its icon's treatment both key off the step
    // state; the --state suffix stays as an interpolated marker.
    const itemTone = { done: "text-secondary", active: "font-medium text-primary", pending: "text-secondary" };
    const iconBase = "claude-login-checklist-icon inline-flex h-[18px] w-[18px] flex-none items-center justify-center";
    const iconTone = {
      done: "text-success",
      active: "",
      // The pending dot is the icon span's ::before, drawn from currentColor.
      pending: "before:h-1.5 before:w-1.5 before:rounded-full before:bg-current before:opacity-40 before:content-['']",
    };
    return m("div", { class: "claude-login-applying py-2" }, [
      m(
        "ul",
        { class: "claude-login-checklist flex flex-col gap-2.5" },
        steps.map((step) =>
          m(
            "li",
            {
              class:
                `claude-login-checklist-item claude-login-checklist-item--${step.state} ` +
                `flex items-center gap-2.5 text-(length:--font-size-body) ${itemTone[step.state]}`,
            },
            [
              m(
                "span",
                { class: `${iconBase} ${iconTone[step.state]}` },
                step.state === "done"
                  ? m.trust(icon("check", { size: 14, strokeWidth: 2.5 }))
                  : step.state === "active"
                    ? m.trust(loginSpinnerIcon())
                    : null,
              ),
              m("span.claude-login-checklist-label", step.label),
            ],
          ),
        ),
      ),
      detail !== null ? m("p", { class: `${HELPER_CLASS} text-center` }, detail) : null,
      renderOldKeyServicesNote(),
    ]);
  }

  // Shown while (and after) switching away from an API-key sign-in: services
  // integrated against the API snapshot the key at their setup time, so the
  // workspace-level switch does not re-point them.
  function renderOldKeyServicesNote(): m.Vnode | null {
    if (!switchedAwayFromApiKey) return null;
    return m(
      "p",
      { class: `${HELPER_CLASS} text-center` },
      "Any existing API integrated services will continue using the old API key. " +
        "Ask the agent to remove those if you need to.",
    );
  }

  function renderStatus(kind: "loading" | "success" | "error", title: string, detail: string | null): m.Vnode {
    const statusGlyph =
      kind === "loading"
        ? m.trust(loginSpinnerIcon())
        : kind === "success"
          ? m.trust(icon("check", { size: 26, strokeWidth: 2.5 }))
          : m.trust(warningIcon());
    const iconTone = {
      loading: "bg-transparent text-accent",
      success: "bg-accent-light text-accent",
      error: "bg-danger-surface text-danger",
    };
    return m("div", { class: "claude-login-status flex flex-col items-center px-2 pt-4 pb-2 text-center" }, [
      m(
        "div",
        {
          class:
            `claude-login-status-icon claude-login-status-icon--${kind} ` +
            `mb-3.5 flex h-[52px] w-[52px] items-center justify-center rounded-full ${iconTone[kind]}`,
        },
        statusGlyph,
      ),
      m(
        "p",
        { class: "claude-login-status-title mb-1 text-(length:--font-size-heading) font-semibold text-primary" },
        title,
      ),
      detail !== null
        ? m(
            "p",
            {
              class:
                "claude-login-status-detail max-w-[320px] text-(length:--font-size-body) leading-normal text-secondary",
            },
            detail,
          )
        : null,
    ]);
  }

  function renderSuccess(): m.Vnode {
    const status = successStatus;
    const email = status?.email ?? null;
    if (status?.auth_mode === "subscription") {
      const detail = email
        ? `Signed in as ${email} with your Claude subscription.`
        : "Signed in with your Claude subscription.";
      return m("div", [
        renderStatus("success", "All set", detail),
        // Only the minted-token flow carries an expiry worth mentioning.
        activeFlow === "setup_token"
          ? m("p", { class: `${HELPER_CLASS} text-center` }, "Your sign-in token is valid for about a year.")
          : null,
        renderOldKeyServicesNote(),
      ]);
    }
    let detail: string;
    if (status?.auth_mode === "console") {
      detail = "Signed in with your Anthropic Console account.";
    } else if (status?.auth_mode === "imbue") {
      detail = "Signed in with Imbue.";
    } else if (email) {
      detail = `Signed in as ${email}.`;
    } else {
      detail = "You're signed in.";
    }
    return m("div", [renderStatus("success", "All set", detail), renderOldKeyServicesNote()]);
  }

  function renderInlineError(): m.Vnode {
    return m(
      "div",
      {
        class:
          "claude-login-error-callout mb-3.5 flex items-start gap-2.5 rounded-lg border border-danger-border " +
          "bg-danger-surface px-3 py-2.5 text-(length:--font-size-body) leading-[1.45] text-danger " +
          "[&>svg]:mt-px [&>svg]:flex-none",
      },
      [m.trust(warningIcon(16)), m("span", errorMessage ?? "")],
    );
  }

  // ----- Layout (header / body / footer) -----

  function titleForMode(): string {
    if (mode === "success") return "Signed in";
    if (mode === "error") return "Something went wrong";
    if (mode === "verifying") return "Just a moment";
    if (mode === "applying") return "Finishing up";
    if (mode === "api_key_form") return "Sign in with API key";
    if (mode === "imbue_form") return "Sign in with Imbue";
    if (mode === "awaiting_setup_token") return "Finish signing in";
    return "Sign in to Claude";
  }

  function renderBody(): m.Vnode | Array<m.Vnode | null> {
    if (mode === "success") return renderSuccess();
    if (mode === "error") {
      return renderStatus("error", "Couldn't complete sign-in", errorMessage ?? "An unexpected error occurred.");
    }
    if (mode === "verifying") return renderStatus("loading", verifyingTitle, verifyingDetail);
    if (mode === "applying") return renderApplying();
    if (mode === "awaiting_setup_token") return renderAwaitingSetupToken();
    if (mode === "api_key_form") return renderApiKeyForm();
    if (mode === "imbue_form") return renderImbueForm();
    return renderProviderSelection();
  }

  // The Back/primary pairs spread to the footer's edges; single-action
  // footers keep their button at the right edge.
  function renderFooterRow(spread: boolean, children: m.Children): m.Vnode {
    return m(
      "div",
      {
        class: `${FOOTER_BASE} ${spread ? "claude-login-footer--spread justify-between" : "justify-end"}`,
      },
      children,
    );
  }

  function renderFooter(): m.Vnode | null {
    if (mode === "select_provider" || mode === "verifying" || mode === "applying") return null;
    if (mode === "success") {
      return renderFooterRow(false, [m(Button, { variant: "primary", onclick: () => attrsRef?.onDismiss() }, "Done")]);
    }
    if (mode === "error") {
      // A sign-in failure (failed setup-token start, or a consumed
      // single-use session) leaves no live session to retry against, so
      // the only forward action is to start the whole flow over. The
      // header close button and backdrop click still dismiss the modal, so
      // a single primary action here is not a dead end.
      return renderFooterRow(false, [
        m(
          Button,
          {
            variant: "primary",
            block: true,
            onclick: () => goBackToProviderSelection(),
          },
          "Start over",
        ),
      ]);
    }
    if (mode === "api_key_form") {
      return renderFooterRow(true, [
        m(Button, { onclick: () => goBackToProviderSelection() }, "Back"),
        m(
          Button,
          {
            variant: "primary",
            disabled: !apiKey.trim(),
            onclick: () => {
              submitApiKey();
            },
          },
          "Save & finish",
        ),
      ]);
    }
    if (mode === "imbue_form") {
      return renderFooterRow(true, [
        m(Button, { onclick: () => goBackToProviderSelection() }, "Back"),
        m(
          Button,
          {
            variant: "primary",
            disabled: !imbueBlob.trim(),
            onclick: () => {
              submitImbueBlob();
            },
          },
          "Save & finish",
        ),
      ]);
    }
    // awaiting_setup_token: no primary action -- the flow completes via
    // polling (or one of the subtle affordances, which carry their own
    // buttons). Back returns to provider selection and aborts the session.
    return renderFooterRow(false, [m(Button, { onclick: () => goBackToProviderSelection() }, "Back")]);
  }

  return {
    oncreate(vnode: m.VnodeDOM<ClaudeLoginModalAttrs>) {
      attrsRef = vnode.attrs;
      loadCurrentStatus();
    },

    onupdate(vnode: m.VnodeDOM<ClaudeLoginModalAttrs>) {
      attrsRef = vnode.attrs;
    },

    onremove() {
      abortSetupTokenIfActive();
      stopApplyingPoll();
      clearMintAckWait();
    },

    view() {
      const onClose = (): void => attrsRef?.onDismiss();
      return m(
        "div",
        { class: OVERLAY_CLASS, ...backdropDismissAttrs(onClose) },
        m(
          "div",
          {
            class: MODAL_CLASS,
            role: "dialog",
            "aria-modal": "true",
            "aria-label": "Sign in to Claude",
          },
          [
            m(
              "div",
              {
                class:
                  "claude-login-header flex items-center justify-between border-b border-subtle px-5.5 pt-4.5 pb-3.5",
              },
              [
                m(
                  "h2",
                  {
                    class:
                      "claude-login-title text-(length:--font-size-heading) font-semibold tracking-[-0.005em] text-primary",
                  },
                  titleForMode(),
                ),
                m(
                  Button,
                  {
                    variant: "ghost",
                    sm: true,
                    icon: true,
                    // -m-1 keeps the 28px hit target from widening the header row.
                    extra: "claude-login-close -m-1",
                    onclick: onClose,
                    "aria-label": "Close",
                  },
                  m.trust(icon("close", { size: 16 })),
                ),
              ],
            ),
            m(
              "div",
              { class: "claude-login-body flex-1 overflow-y-auto px-5.5 pt-4.5 pb-1" },
              mode === "awaiting_setup_token" || mode === "api_key_form" || mode === "imbue_form"
                ? [errorMessage !== null ? renderInlineError() : null, renderBody()]
                : renderBody(),
            ),
            renderFooter(),
          ],
        ),
      );
    },
  };
}
