/**
 * The agent permission-request card: a self-contained component that renders a
 * latchkey permission request as a human-readable card (what's being requested,
 * the agent's reason, a review button or the user's verdict, and a raw-request
 * disclosure) rather than a generic "Tool: Bash" block. A pending request for a
 * service the user's provider account already has connected also carries a
 * quiet offer to turn those connections on instead (`cloud-connections-offer.ts`).
 *
 * The card has one seam: `PermissionCard` is the live component (it parses the
 * request once and looks up the gateway catalog so a scope like `slack-api`
 * shows as "Slack"), and `renderPermissionCard` is the pure renderer it delegates
 * to once details and scope info are in hand. Keeping the pure renderer separate
 * lets tests inject `scopeInfo` synchronously without driving the async lookup.
 */

import m from "mithril";
import { OPEN_REQUEST_MODAL } from "@minds/embed-contract";
import type { ContractMessage } from "@minds/embed-contract";
import { PERMISSION_RESOLUTIONS, sendToEmbedder, setEmbedderMessageHandler } from "@imbue/workspace-ui/src/embed";
import type { ToolCall, ToolResultEvent } from "../models/Response";
import { getEventDetailState, requestEventDetail } from "../models/Response";
import type { CloudConnectionsOffer } from "./cloud-connections-offer";
import {
  enableCloudConnections,
  getCloudConnectionsOffer,
  isCloudConnectionsOfferTaken,
} from "./cloud-connections-offer";
import type { ScopeInfo } from "./latchkey-scope-info";
import { getScopeInfo } from "./latchkey-scope-info";
import type { PermissionResolution } from "./message-classification";
import { isPermissionRequestCall } from "./message-classification";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { IconName } from "@imbue/workspace-ui/src/components/icons";
import { serviceMarkUrl } from "./service-marks";
import { Button } from "@imbue/workspace-ui/src/components/Button";

/** The rich fields a created permission request echoes back on stdout, parsed
 *  from the tool result. `requestId` is always present (it's what the modal
 *  button needs); the rest depend on the request type. */
export interface PermissionRequestDetails {
  requestId: string;
  /** "predefined" (a service scope) or "file-sharing", or null if absent. */
  requestType: string | null;
  /** The agent's human-readable reason for the request. */
  rationale: string | null;
  /** Predefined requests: the latchkey scope (e.g. "slack-api") and the
   *  specific permissions being granted. */
  scope: string | null;
  permissions: string[];
  /** File-sharing requests: the path and access mode (READ/WRITE). */
  path: string | null;
  access: string | null;
}

function asObject(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

/** Map the gateway's response object onto the fields the card renders. The one
 *  place that knows the response's field names, so the backend's structured
 *  field, the strict parse, and the truncation recovery all yield the same
 *  shape. */
function detailsFromResponseObject(obj: Record<string, unknown>): PermissionRequestDetails | null {
  if (typeof obj.request_id !== "string") {
    return null;
  }
  const payload = asObject(obj.payload) ?? {};
  const permissions = Array.isArray(payload.permissions)
    ? payload.permissions.filter((p): p is string => typeof p === "string")
    : [];
  return {
    requestId: obj.request_id,
    requestType: typeof obj.request_type === "string" ? obj.request_type : null,
    rationale: typeof obj.rationale === "string" ? obj.rationale : null,
    scope: typeof payload.scope === "string" ? payload.scope : null,
    permissions,
    path: typeof payload.path === "string" ? payload.path : null,
    access: typeof payload.access === "string" ? payload.access : null,
  };
}

/**
 * Parse the rich details of a *successful* latchkey permission-request creation
 * call out of its tool result.
 *
 * An agent asks the user for permission by POSTing to the reserved
 * `latchkey-self.invalid/permission-requests` host (see the connect-external-service
 * skill's `references/latchkey.md`).
 * The created request's JSON -- request_id, rationale, request_type, and a
 * type-specific payload -- routinely runs past the transcript's per-result
 * output limit, so it is read from the `permission_request` field the backend
 * parsed off the untruncated output; there is no output-scanning fallback.
 * Returns null when the call is not such a creation POST, errored, or carries
 * no structured field (a transcript from an older backend) -- the caller then
 * shows the honest can't-read state pointing at the Permissions tab.
 */
export function parsePermissionRequest(
  toolCall: ToolCall,
  toolResult: ToolResultEvent | null,
): PermissionRequestDetails | null {
  // The same input-only predicate the timeline walk uses to lift the request
  // out of its step, so the two stay in lockstep.
  if (!isPermissionRequestCall(toolCall)) {
    return null;
  }
  if (!toolResult || toolResult.is_error === true) {
    return null;
  }
  const structured = asObject(toolResult.permission_request);
  return structured === null ? null : detailsFromResponseObject(structured);
}

/** Whether this tool call is a permission request the gateway actually FILED.
 *
 *  The card renders for a request the user can act on (or has acted on). A
 *  call the harness refused, one whose curl failed outright, or one the
 *  gateway rejected (`curl` exits 0 on a 4xx, so the call does not even read
 *  as failed) produced no pending request -- rendering those as a permission
 *  card sends the user to a Permissions tab that has nothing in it, so they
 *  render as the ordinary tool calls they are.
 *
 *  A call with no result YET does count: that is the request in flight, which
 *  is exactly when it most needs to be visible.
 */
export function isFiledPermissionRequest(toolCall: ToolCall, toolResult: ToolResultEvent | null): boolean {
  if (!isPermissionRequestCall(toolCall)) return false;
  if (toolResult === null) return true;
  return parsePermissionRequest(toolCall, toolResult) !== null;
}

/**
 * Ask the outer Imbue Studio app to open its permission-request modal. The chat UI
 * runs inside an iframe, so we hand the request id to the embedding chrome
 * via the embed contract rather than rendering the modal ourselves.
 */
export function openPermissionRequest(requestId: string): void {
  sendToEmbedder(OPEN_REQUEST_MODAL, { requestId });
}

// Shell-reported verdicts
//
// Verdicts learned over `minds:permission-resolutions`, which arrives two ways
// with one meaning: unsolicited with a single entry the moment the user
// resolves a request in the review popup (the card flips ahead of the
// resolution message's transcript round trip), and as the recent-verdicts
// snapshot the chrome pushes whenever this page (re)loads -- this in-memory
// cache dies with the page, and without the snapshot a rebuilt page would
// offer Approve/Deny for a request decided while it was not live. The
// endpoint admits the message only from this page's embedder with a
// well-shaped payload; the transcript's classified resolution takes over once
// it lands. Sends need no vendored-contract support but a stale vendored
// endpoint drops the message until the sync lands, leaving cards
// transcript-driven (also the direct-share behavior, where no embedder
// exists).
const shellResolutions = new Map<string, PermissionResolution>();

// When THIS page first learned each verdict. The shell's verdict lands well
// before the agent does -- the desktop client flips the card from its own
// record and only then starts trying to deliver the resolution message into
// the agent's session, which is a multi-step, retried delivery (see
// `MngrMessageSender` in the mngr repo). The gap is the window in which the
// user has decided and nothing is visibly happening, so the activity strip
// spins a bare dot through it (see `wakeUpSpinnerDeadline`). Recorded only on
// first sight of a request id, so the load-time snapshot's idempotent re-push
// cannot restart the clock. Times are page-local: this map dies with the page,
// exactly like `shellResolutions` above.
const shellResolutionArrivals = new Map<string, number>();

/** The shell-reported verdict for a request, or null if the shell hasn't
 *  reported one. */
export function shellPermissionResolutionFor(requestId: string): PermissionResolution | null {
  return shellResolutions.get(requestId) ?? null;
}

/** When this page first learned a shell-reported verdict for `requestId`
 *  (`Date.now()` at the time), or null if it never has. */
export function shellResolutionArrivalFor(requestId: string): number | null {
  return shellResolutionArrivals.get(requestId) ?? null;
}

/** Whether any shell-reported verdict landed after `since`. A cheap pre-check
 *  (the map holds one entry per request this page has seen resolved) so the
 *  activity strip can skip its transcript scan on the overwhelmingly common
 *  redraw where nothing was just resolved. */
export function hasShellResolutionSince(since: number): boolean {
  for (const arrival of shellResolutionArrivals.values()) {
    if (arrival > since) return true;
  }
  return false;
}

/** Record every verdict a `minds:permission-resolutions` message carries --
 *  a query answer or the shell's unsolicited single-entry push. */
export function notePermissionResolutions(message: ContractMessage): void {
  const { resolutions } = message;
  if (!Array.isArray(resolutions)) return;
  let isAnyRecorded = false;
  for (const entry of resolutions) {
    if (typeof entry !== "object" || entry === null) continue;
    const { requestId, resolution } = entry as ContractMessage;
    if (typeof requestId !== "string" || requestId === "") continue;
    if (resolution !== "granted" && resolution !== "denied") continue;
    // First sight only: the snapshot is pushed up to three times per page load
    // and re-pushed on every reload, and a re-notification of a verdict this
    // page already knows is not a fresh decision to wait on.
    if (!shellResolutionArrivals.has(requestId)) shellResolutionArrivals.set(requestId, Date.now());
    shellResolutions.set(requestId, resolution);
    isAnyRecorded = true;
  }
  if (isAnyRecorded) m.redraw();
}

/** Drop the verdict cache so the next test starts from a quiet page. */
export function resetShellPermissionResolutionsForTesting(): void {
  shellResolutions.clear();
  shellResolutionArrivals.clear();
}

/** Subscribe the cards to the shell's verdicts. Called once at app bootstrap. */
export function initShellPermissionResolutions(): void {
  setEmbedderMessageHandler(PERMISSION_RESOLUTIONS, notePermissionResolutions);
}

/* Card styling.
 * The card is styled with utilities at each call site below; the
 * permission-request-* class names stay as bare markers (the vitest suites and
 * the e2e tests query them). Shared fragments live here so the two card badges
 * and the two raw-toggle placements can't drift. */

/** The card shell every state shares. */
const CARD_CLASS = "permission-request max-w-[560px] rounded-lg border bg-composer px-3.5 py-3";

/** The rounded subject badge; the pending body uses the 32px form, the
 *  resolved receipt the 24px one. Size + radius are resolved per call site so
 *  no two utilities fight over one property. */
const BADGE_BASE = "flex shrink-0 items-center justify-center bg-fill-hover text-secondary";

/** The verdict's color says the outcome: approved reads in the accent green;
 *  denied reads neutral (a decision, not an error), as does a request set aside
 *  for the account's own connections; couldn't-complete warns. */
const VERDICT_COLOR: Record<PermissionResolution, string> = {
  granted: "text-accent",
  denied: "text-secondary",
  error: "text-warning",
  cloud_connections: "text-secondary",
};

const VERDICT_ICON: Record<PermissionResolution, IconName> = {
  granted: "check",
  denied: "close",
  error: "alert",
  cloud_connections: "zap",
};

/** "error" reads as "Couldn't complete" -- the request didn't finish, distinct
 *  from a deny decision. */
const VERDICT_LABEL: Record<PermissionResolution, string> = {
  granted: "Approved",
  denied: "Denied",
  error: "Couldn't complete",
  cloud_connections: "Cloud connections enabled instead",
};

/** The key that heads every card state's eyebrow. 13px is the size of the
 *  verdict glyphs it sits level with on the receipt row. */
function renderKeyIcon(): m.Vnode {
  return m.trust(icon("key", { size: 13, className: "permission-request-icon shrink-0" }));
}

/**
 * The badge's subject mark: the requested service's own logo when we bundle
 * one, and the generic cube otherwise. File-sharing, workspace and accounts
 * requests name no app by definition, and a service we ship no artwork for
 * falls back the same way.
 *
 * The logo is an `<img>`, never inlined: the artwork carries its own color, and
 * several marks pair a white path with a deliberately unfilled one, so a
 * `fill: currentColor` ancestor would repaint them.
 */
function renderSubjectMark(details: PermissionRequestDetails | null, size: number): m.Vnode {
  const markUrl = details?.scope ? serviceMarkUrl(details.scope) : null;
  if (markUrl === null) {
    return m.trust(icon("box", { size, className: "permission-request-icon shrink-0" }));
  }
  // object-contain matters because several of the marks are non-square.
  return m("img", {
    src: markUrl,
    alt: "",
    width: size,
    height: size,
    class: "permission-request-mark block shrink-0 object-contain",
  });
}

/** The generic subject, used only where a row would otherwise be blank. It
 *  repeats the eyebrow, so it is a last resort rather than a default. */
const GENERIC_PERMISSION_TITLE = "Permission request";

/** The card title: what's being asked for, in a few words. "Local files" for a
 *  file-sharing request; "Other workspaces" for a workspace request (acting on the
 *  user's other Imbue Studio workspaces); "Device accounts" for an accounts request;
 *  "This computer" for a remote-execution request (running commands on the user's computer);
 *  the friendly service name for a predefined request once the gateway catalog
 *  resolves (the raw scope until then); null when nothing named the subject, so
 *  each caller decides whether a generic stand-in beats no row at all. The
 *  specifics live in the review modal and the raw disclosure. */
function permissionTitle(details: PermissionRequestDetails | null, scopeInfo: ScopeInfo | null): string | null {
  if (details?.requestType === "file-sharing") return "Local files";
  if (details?.requestType === "workspace") return "Other workspaces";
  if (details?.requestType === "accounts") return "Device accounts";
  if (details?.requestType === "remote-execution") return "This computer";
  if (details?.scope) return scopeInfo?.display_name ?? details.scope;
  return null;
}

/** A small glyph for the resolved-request verdict. */
function renderVerdictIcon(resolution: PermissionResolution): m.Vnode {
  return m.trust(icon(VERDICT_ICON[resolution], { size: 13, className: "permission-request-verdict-icon shrink-0" }));
}

/** The resolved verdict shown at the right of the receipt row (approved,
 *  denied, or could-not-complete). */
function renderPermissionVerdict(resolution: PermissionResolution): m.Vnode {
  return m(
    "div",
    {
      // The modifier marker is interpolated (markers are invisible to the
      // Tailwind scanner); the color utility comes from the literal map above.
      class:
        `permission-request-verdict permission-request-verdict--${resolution} ` +
        `inline-flex shrink-0 items-center gap-[5px] text-(length:--font-size-helper) font-semibold ` +
        VERDICT_COLOR[resolution],
    },
    [renderVerdictIcon(resolution), m("span", VERDICT_LABEL[resolution])],
  );
}

/** The eyebrow row every card state shares: a small key + "Permission request". */
function renderEyebrow(): m.Vnode {
  return m(
    "div",
    { class: "permission-request-eyebrow flex items-center gap-1.5 text-(length:--font-size-helper) text-secondary" },
    [renderKeyIcon(), m("span", "Permission request")],
  );
}

/** The plain-text "Show raw request" / "Hide raw request" toggle, or null when
 *  there's no raw text to disclose. */
function renderRawToggle(rawText: string, rawOpen: boolean, onToggleRaw: () => void): m.Vnode | null {
  if (!rawText) return null;
  return m(
    "button",
    {
      class:
        "permission-request-raw-toggle cursor-pointer border-none bg-transparent p-0 " +
        "text-(length:--font-size-helper) text-faint transition-[color] duration-(--dur-base) hover:text-secondary",
      type: "button",
      onclick(e: Event) {
        e.preventDefault();
        e.stopPropagation();
        onToggleRaw();
      },
    },
    rawOpen ? "Hide raw request" : "Show raw request",
  );
}

/** The raw request/response block, shown full-width when the toggle is open. */
function renderRawBlock(rawText: string, rawOpen: boolean): m.Vnode | null {
  if (!rawText || !rawOpen) return null;
  return m(
    "div",
    { class: "permission-request-raw mt-2.5" },
    m(
      "pre",
      {
        class:
          "overflow-auto rounded-md bg-sidebar p-2 font-mono text-(length:--font-size-helper) whitespace-pre-wrap text-secondary",
      },
      m("code", rawText),
    ),
  );
}

/** The offer of the account's own cloud connections, as the live component
 *  hands it to the renderer: whose account, whether the warning is showing,
 *  and the enable request's progress. */
export interface CloudConnectionsOfferView {
  providerName: string;
  isOpen: boolean;
  isBusy: boolean;
  error: string | null;
  onToggle: () => void;
  onEnable: () => void;
}

/** The offer under a pending card's actions: one quiet line, which opens onto
 *  a warning and the button that takes it. */
function renderCloudConnectionsOffer(offer: CloudConnectionsOfferView): m.Vnode {
  return m("div", { class: "permission-request-cloud-offer mt-2.5" }, [
    m(
      "button",
      {
        class:
          "permission-request-cloud-offer-toggle cursor-pointer border-none bg-transparent p-0 text-left " +
          "text-(length:--font-size-helper) text-faint transition-[color] duration-(--dur-base) hover:text-secondary",
        type: "button",
        "aria-expanded": offer.isOpen ? "true" : "false",
        onclick(e: Event) {
          e.preventDefault();
          e.stopPropagation();
          offer.onToggle();
        },
      },
      `Alternatively, you could enable the existing connections associated with your ${offer.providerName} account.`,
    ),
    offer.isOpen
      ? m("div", { class: "permission-request-cloud-offer-detail mt-2 flex flex-col items-start gap-2" }, [
          m(
            "div",
            {
              class:
                "permission-request-cloud-offer-warning flex items-start gap-1.5 " +
                "text-(length:--font-size-helper) leading-[1.45] text-warning",
            },
            [
              m.trust(icon("alert", { size: 13, className: "mt-0.5 shrink-0" })),
              m(
                "span",
                "This circumvents Imbue Studio's permission guarantees: every agent in this workspace will be able to " +
                  `use every service connected on your ${offer.providerName} account, or on whichever account it ` +
                  "runs on, without the permissions you grant here. This chat restarts to apply it; other chats " +
                  "pick it up when they next restart.",
              ),
            ],
          ),
          m(
            Button,
            {
              variant: "secondary",
              disabled: offer.isBusy,
              onclick(e: Event) {
                e.preventDefault();
                e.stopPropagation();
                offer.onEnable();
              },
            },
            offer.isBusy ? "Restarting the chat\u2026" : "Enable existing cloud connections",
          ),
          offer.error !== null
            ? m(
                "div",
                { class: "permission-request-cloud-offer-error text-(length:--font-size-helper) text-warning" },
                offer.error,
              )
            : null,
        ])
      : null,
  ]);
}

/**
 * Pure renderer for the permission card, given the already-parsed request
 * `details`, the resolved gateway `scopeInfo` (or null before it lands), the
 * user's `resolution` (or null while pending), the `rawText` for the raw
 * disclosure, the disclosure's open state (`rawOpen` + `onToggleRaw`,
 * owned by the live component), and the offer of the account's own cloud
 * connections when the backend makes one (`cloudOffer`, drawn only on a
 * pending, parsed card). The live `PermissionCard` component computes these
 * once and calls here; tests call it directly with an injected `scopeInfo`.
 *
 * Three states, all under the same eyebrow row:
 *   - Resolved (`resolution` non-null): a compact one-line receipt -- badge,
 *     title, and the Approved / Denied / Couldn't-complete verdict.
 *   - Pending and parsed (`details` non-null): badge, title, the agent's
 *     rationale, and a solid "Review & respond" button that opens the modal.
 *   - Pending and unparsed (`details` null): `hasResult` picks between the two
 *     buttonless status lines -- the result hasn't arrived yet (still
 *     waiting), or it arrived but no request id could be read from it (so the
 *     card says so honestly instead of waiting forever).
 */
export function renderPermissionCard(
  details: PermissionRequestDetails | null,
  scopeInfo: ScopeInfo | null,
  resolution: PermissionResolution | null,
  rawText: string,
  hasResult: boolean,
  rawOpen: boolean,
  onToggleRaw: () => void,
  cloudOffer: CloudConnectionsOfferView | null = null,
): m.Vnode {
  const title = permissionTitle(details, scopeInfo);
  const rawToggle = renderRawToggle(rawText, rawOpen, onToggleRaw);
  const rawBlock = renderRawBlock(rawText, rawOpen);

  if (resolution !== null) {
    // One compact line: the verdict reads inline right after the title, and
    // the raw toggle keeps to the right edge of the same row, so the receipt
    // never grows a second row.
    return m("div", { class: CARD_CLASS }, [
      renderEyebrow(),
      m("div", { class: "permission-request-receipt mt-2.5 flex items-center gap-2" }, [
        m(
          "div",
          { class: `permission-request-badge permission-request-badge--sm h-6 w-6 rounded-md ${BADGE_BASE}` },
          renderSubjectMark(details, 14),
        ),
        m(
          "div",
          {
            class: "permission-request-receipt-title min-w-0 truncate type-label text-primary",
          },
          title ?? GENERIC_PERMISSION_TITLE,
        ),
        renderPermissionVerdict(resolution),
        rawToggle ? m("div", { class: "permission-request-receipt-toggle ml-auto shrink-0" }, rawToggle) : null,
      ]),
      rawBlock,
    ]);
  }

  if (details === null) {
    return m("div", { class: CARD_CLASS }, [
      renderEyebrow(),
      m(
        "div",
        { class: "permission-request-status mt-[9px] text-(length:--font-size-body) text-secondary" },
        hasResult ? "Couldn't read this request — see the Permissions tab." : "Waiting for the request to register…",
      ),
      rawToggle ? m("div", { class: "permission-request-toggle-row mt-2 flex justify-end" }, rawToggle) : null,
      rawBlock,
    ]);
  }

  // The body must never be a bare badge: fall back to the generic subject only
  // when no rationale survived either, so a titled-but-redundant row never sits
  // under an identical eyebrow.
  const bodyTitle = title ?? (details.rationale === null ? GENERIC_PERMISSION_TITLE : null);
  return m("div", { class: CARD_CLASS }, [
    renderEyebrow(),
    m("div", { class: "permission-request-body mt-2.5 flex items-start gap-2.5" }, [
      m("div", { class: `permission-request-badge h-8 w-8 rounded-lg ${BADGE_BASE}` }, renderSubjectMark(details, 16)),
      m("div", { class: "permission-request-info min-w-0 flex-1" }, [
        bodyTitle !== null ? m("div", { class: "permission-request-title type-label text-primary" }, bodyTitle) : null,
        details.rationale
          ? m(
              "div",
              { class: "permission-request-reason text-(length:--font-size-helper) leading-[1.45] text-secondary" },
              details.rationale,
            )
          : null,
      ]),
    ]),
    m("div", { class: "permission-request-actions mt-3 flex items-center justify-between gap-2.5" }, [
      m(
        Button,
        {
          variant: "primary",
          onclick(e: Event) {
            e.preventDefault();
            e.stopPropagation();
            openPermissionRequest(details.requestId);
          },
        },
        "Review & respond",
      ),
      rawToggle,
    ]),
    cloudOffer !== null ? renderCloudConnectionsOffer(cloudOffer) : null,
    rawBlock,
  ]);
}

/**
 * The live permission-request card. Parses the request once, resolves its
 * service scope to the gateway catalog (the service display name shown as the
 * card title) -- a cache-guarded async lookup, so the card first renders with
 * the raw scope and updates once the catalog resolves -- holds the raw
 * disclosure's open/closed state, and delegates to `renderPermissionCard`.
 * Predefined requests have a scope to resolve; file-sharing requests don't.
 * It also asks whether the backend offers the account's own cloud connections
 * for that scope, and owns the offer's open state and its enable request.
 */
export function PermissionCard(): m.Component<{
  toolCall: ToolCall;
  toolResult: ToolResultEvent | null;
  resolution: PermissionResolution | null;
  // For the raw disclosure's on-demand payload fetch (the wire is payload-free).
  chatId: string;
  assistantEventId: string;
}> {
  let rawOpen = false;
  let isOfferOpen = false;
  let isOfferBusy = false;
  let offerError: string | null = null;

  async function takeOffer(chatId: string, requestId: string): Promise<void> {
    isOfferBusy = true;
    offerError = null;
    m.redraw();
    const outcome = await enableCloudConnections(chatId, requestId);
    isOfferBusy = false;
    offerError = outcome.ok ? null : outcome.detail;
    m.redraw();
  }

  function offerView(
    chatId: string,
    details: PermissionRequestDetails,
    offer: CloudConnectionsOffer,
  ): CloudConnectionsOfferView {
    return {
      providerName: offer.provider_name ?? "provider",
      isOpen: isOfferOpen,
      isBusy: isOfferBusy,
      error: offerError,
      onToggle: () => {
        isOfferOpen = !isOfferOpen;
      },
      onEnable: () => {
        void takeOffer(chatId, details.requestId);
      },
    };
  }

  return {
    view(vnode) {
      const { toolCall, toolResult, resolution, chatId, assistantEventId } = vnode.attrs;
      const details = parsePermissionRequest(toolCall, toolResult);
      const scopeInfo = details?.scope ? getScopeInfo(details.scope) : null;
      // The raw disclosure fetches the full input/output on open (cached frontend-side);
      // until they land, the structured request object stands in.
      if (rawOpen) {
        if (toolCall.input_chars > 0) {
          requestEventDetail(chatId, assistantEventId);
        }
        if (toolResult && toolResult.output_chars > 0) {
          requestEventDetail(chatId, toolResult.event_id);
        }
      }
      const inputDetail = rawOpen ? getEventDetailState(chatId, assistantEventId) : undefined;
      const outputDetail = rawOpen && toolResult ? getEventDetailState(chatId, toolResult.event_id) : undefined;
      const rawInput =
        inputDetail?.state === "loaded"
          ? (inputDetail.detail.inputs_by_tool_call_id[toolCall.tool_call_id] ?? "")
          : "";
      const fallbackRaw = toolResult?.permission_request
        ? JSON.stringify(toolResult.permission_request, null, 2)
        : toolResult
          ? "Open to load the raw output\u2026"
          : "";
      const rawOutput = outputDetail?.state === "loaded" ? (outputDetail.detail.output ?? "") : fallbackRaw;
      const rawText = rawOutput ? `${rawInput}\n\n${rawOutput}` : rawInput;
      // The transcript-classified resolution wins; before it lands, an offer
      // this page took, or a verdict the shell reported for this request (the
      // user just resolved it in the review popup), flips the card without
      // waiting for the round trip.
      const pageResolution =
        details === null
          ? null
          : isCloudConnectionsOfferTaken(details.requestId)
            ? "cloud_connections"
            : shellPermissionResolutionFor(details.requestId);
      const effectiveResolution = resolution ?? pageResolution;
      // Only a pending request for a service can be set aside for the
      // account's own connection to that service.
      const offer =
        effectiveResolution === null && details?.scope ? getCloudConnectionsOffer(chatId, details.scope) : null;
      return renderPermissionCard(
        details,
        scopeInfo,
        effectiveResolution,
        rawText,
        toolResult !== null,
        rawOpen,
        () => {
          rawOpen = !rawOpen;
        },
        details !== null && offer !== null ? offerView(chatId, details, offer) : null,
      );
    },
  };
}
