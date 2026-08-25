/**
 * The agent permission-request card: a self-contained component that renders a
 * latchkey permission request as a human-readable card (what's being requested,
 * the agent's reason, a review button or the user's verdict, and a raw-request
 * disclosure) rather than a generic "Tool: Bash" block.
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
import { PERMISSION_REQUEST_RESOLVED, sendToEmbedder, setEmbedderMessageHandler } from "../embed";
import type { ToolCall, ToolResultEvent } from "../models/Response";
import type { ScopeInfo } from "./latchkey-scope-info";
import { getScopeInfo } from "./latchkey-scope-info";
import type { PermissionResolution } from "./message-classification";
import { isPermissionRequestCall } from "./message-classification";
import { icon } from "./icons";
import type { IconName } from "./icons";
import { serviceMarkUrl } from "./service-marks";

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

/** The gateway generates every request_id as a dash-stripped UUIDv4 (see
 *  `generateRequestId` in the latchkey extension's permission_requests.mjs). The
 *  strict parse doesn't need this -- JSON structure already proves the value is
 *  the response's request_id -- but the truncation recovery below reconstructs
 *  its text, so it demands the id also *look* gateway-minted before the modal is
 *  opened with it. */
const GENERATED_REQUEST_ID_PATTERN = /^[0-9a-f]{32}$/;

function asObject(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

/**
 * The longest prefix of `body` that is a complete JSON object, re-closed, or
 * null if not even its first member completed.
 *
 * Truncation cuts the response mid-object so `JSON.parse` throws on the whole
 * thing. But the gateway writes the fields this card needs first and the bulk
 * that overflows the limit (`target`, `effect`) last -- see handleCreateRequest
 * in the latchkey extension -- so every member up to the last completed one is
 * intact. Cut at that boundary (the last `,` at nesting depth 1, outside any
 * string) and close the object.
 *
 * The walk only ever DISCARDS a trailing member and appends `}`: it never
 * invents a key nor lifts a value out of its context, so the repaired object is
 * always a subset of what the sender wrote.
 */
function completeTopLevelPrefix(body: string): string | null {
  let depth = 0;
  let inString = false;
  let escaped = false;
  let lastComplete = -1;
  for (let i = 0; i < body.length; i++) {
    const c = body[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (c === "\\") escaped = true;
      else if (c === '"') inString = false;
      continue;
    }
    if (c === '"') inString = true;
    else if (c === "{" || c === "[") depth++;
    else if (c === "}" || c === "]") depth--;
    else if (c === "," && depth === 1) lastComplete = i;
  }
  return lastComplete < 0 ? null : `${body.slice(0, lastComplete)}}`;
}

/** The response object the creation POST echoed, parsed strictly if the output
 *  survived intact and otherwise recovered from its truncated head. */
function parseResponseObject(output: string): Record<string, unknown> | null {
  // curl writes a progress meter before the response body; the JSON object
  // starts at the first `{`.
  const start = output.indexOf("{");
  if (start < 0) {
    return null;
  }
  const body = output.slice(start);
  try {
    return asObject(JSON.parse(body));
  } catch {
    // Truncated mid-object. The backend now preserves the whole object past its
    // output limit, so this is the path for a transcript parsed by an older
    // backend: recover the head rather than dropping the whole card.
  }
  const repaired = completeTopLevelPrefix(body);
  if (repaired === null) {
    return null;
  }
  let recovered: Record<string, unknown> | null;
  try {
    recovered = asObject(JSON.parse(repaired));
  } catch {
    return null;
  }
  if (recovered === null) {
    return null;
  }
  return typeof recovered.request_id === "string" && GENERATED_REQUEST_ID_PATTERN.test(recovered.request_id)
    ? recovered
    : null;
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
 * `latchkey-self.invalid/permission-requests` host (see the latchkey skill).
 * The created request's JSON -- request_id, rationale, request_type, and a
 * type-specific payload -- is echoed back on stdout, after curl's progress
 * meter.
 *
 * That response routinely runs past the transcript's per-result output limit,
 * so it is read from the `permission_request` field the backend parsed off the
 * untruncated output. The output scan below is the fallback for the one case
 * the backend deliberately refuses to preserve: a response past its
 * preservation ceiling (an agent's rationale has no length limit), which
 * arrives with no structured field and a head-truncated body. The scan then
 * repairs what it can of that body rather than abandoning a request the user
 * still has to answer.
 *
 * Returns the parsed details when the call is such a creation POST that
 * succeeded and carries a request_id; otherwise null (the request is still
 * pending, errored, or nothing could be read -- the caller then shows a
 * pending card and keeps the raw output available).
 *
 * The `request_id` this reads is what the "Review & respond" button opens the
 * approval dialog with, so a filing whose echo never reaches its own tool result
 * renders a card the user cannot act on. The PreToolUse gate in
 * `system/scripts/agent_latchkey_request_standalone.sh` blocks the forms that
 * would do that; if this parser learns to read a filing the gate refuses, or
 * stops reading one it allows, the two need updating together.
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
  if (structured !== null) {
    const details = detailsFromResponseObject(structured);
    if (details !== null) {
      return details;
    }
  }
  const parsed = parseResponseObject(toolResult.output || "");
  return parsed === null ? null : detailsFromResponseObject(parsed);
}

/** True when a permission-request call has something for the user to answer.
 *
 *  Filing one is a plain HTTP POST, and plenty of ways to fail leave a tool call
 *  that looks like a filing but created nothing: a PreToolUse guard refusing the
 *  command, or the gateway rejecting the body (`curl` exits 0 on a 4xx, so the
 *  call does not even read as failed). Rendering those as a permission card sends
 *  the user to a Permissions tab that has nothing in it, so they render as the
 *  ordinary tool calls they are -- with whatever the error was.
 *
 *  A call with no result YET does count: that is the request in flight, which is
 *  exactly when it most needs to be visible.
 */
export function isFiledPermissionRequest(toolCall: ToolCall, toolResult: ToolResultEvent | null): boolean {
  if (!isPermissionRequestCall(toolCall)) return false;
  if (toolResult === null) return true;
  return parsePermissionRequest(toolCall, toolResult) !== null;
}

/**
 * Ask the outer Minds app to open its permission-request modal. The chat UI
 * runs inside an iframe, so we hand the request id to the embedding chrome
 * via the embed contract rather than rendering the modal ourselves.
 */
export function openPermissionRequest(requestId: string): void {
  sendToEmbedder(OPEN_REQUEST_MODAL, { requestId });
}

// -- Shell-resolved requests --------------------------------------------------
//
// When the Minds app's review popup resolves a request, the shell sends
// `minds:permission-request-resolved` over the embed contract, which admits it
// only from this page's own embedder and only with a well-shaped payload. The
// matching card flips to its verdict immediately instead of waiting for the
// resolution message's round trip through the agent transcript; once that
// message lands, the classified resolution takes over (and agrees with the
// verdict recorded here).
const shellResolutions = new Map<string, PermissionResolution>();

/** Record the verdict a resolution message carries. Returns whether one was
 *  (so the caller knows to redraw). */
function noteShellPermissionResolution(message: ContractMessage): boolean {
  const { requestId, resolution } = message;
  if (typeof requestId !== "string" || requestId === "") return false;
  if (resolution !== "granted" && resolution !== "denied") return false;
  shellResolutions.set(requestId, resolution);
  return true;
}

/** The shell-reported verdict for a request, or null if the shell hasn't
 *  reported one. */
export function shellPermissionResolutionFor(requestId: string): PermissionResolution | null {
  return shellResolutions.get(requestId) ?? null;
}

/** Subscribe the cards to the shell's verdicts. Called once at app bootstrap. */
export function initShellPermissionResolutions(): void {
  setEmbedderMessageHandler(PERMISSION_REQUEST_RESOLVED, (message) => {
    if (noteShellPermissionResolution(message)) m.redraw();
  });
}

/** The key that heads every card state's eyebrow. 13px is the size of the
 *  verdict glyphs it sits level with on the receipt row. */
function renderKeyIcon(): m.Vnode {
  return m.trust(icon("key", { size: 13, className: "permission-request-icon" }));
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
    return m.trust(icon("box", { size, className: "permission-request-icon" }));
  }
  return m("img", { src: markUrl, alt: "", width: size, height: size, class: "permission-request-mark" });
}

/** The generic subject, used only where a row would otherwise be blank. It
 *  repeats the eyebrow, so it is a last resort rather than a default. */
const GENERIC_PERMISSION_TITLE = "Permission request";

/** The card title: what's being asked for, in a few words. "Local files" for a
 *  file-sharing request; "Other machines" for a workspace request (acting on the
 *  user's other Minds workspaces); "Device accounts" for an accounts request;
 *  the friendly service name for a predefined request once the gateway catalog
 *  resolves (the raw scope until then); null when nothing named the subject, so
 *  each caller decides whether a generic stand-in beats no row at all. The
 *  specifics live in the review modal and the raw disclosure. */
function permissionTitle(details: PermissionRequestDetails | null, scopeInfo: ScopeInfo | null): string | null {
  if (details?.requestType === "file-sharing") return "Local files";
  if (details?.requestType === "workspace") return "Other machines";
  if (details?.requestType === "accounts") return "Device accounts";
  if (details?.scope) return scopeInfo?.display_name ?? details.scope;
  return null;
}

/** A small glyph for the resolved-request verdict: a check (approved), a cross
 *  (denied), or an exclamation (error / couldn't complete). */
function renderVerdictIcon(resolution: PermissionResolution): m.Vnode {
  const name: IconName = resolution === "granted" ? "check" : resolution === "denied" ? "close" : "alert";
  return m.trust(icon(name, { size: 13, className: "permission-request-verdict-icon" }));
}

/** The label shown beside the verdict icon. "error" reads as "Couldn't
 *  complete" -- the request didn't finish, distinct from a deny decision. */
function verdictLabel(resolution: PermissionResolution): string {
  if (resolution === "granted") return "Approved";
  if (resolution === "denied") return "Denied";
  return "Couldn't complete";
}

/** The resolved verdict shown at the right of the receipt row (approved,
 *  denied, or could-not-complete). */
function renderPermissionVerdict(resolution: PermissionResolution): m.Vnode {
  return m("div", { class: `permission-request-verdict permission-request-verdict--${resolution}` }, [
    renderVerdictIcon(resolution),
    m("span", verdictLabel(resolution)),
  ]);
}

/** The eyebrow row every card state shares: a small key + "Permission request". */
function renderEyebrow(): m.Vnode {
  return m("div", { class: "permission-request-eyebrow" }, [renderKeyIcon(), m("span", "Permission request")]);
}

/** The plain-text "Show raw request" / "Hide raw request" toggle, or null when
 *  there's no raw text to disclose. */
function renderRawToggle(rawText: string, rawOpen: boolean, onToggleRaw: () => void): m.Vnode | null {
  if (!rawText) return null;
  return m(
    "button",
    {
      class: "permission-request-raw-toggle",
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
  return m("div", { class: "permission-request-raw" }, m("pre", m("code", rawText)));
}

/**
 * Pure renderer for the permission card, given the already-parsed request
 * `details`, the resolved gateway `scopeInfo` (or null before it lands), the
 * user's `resolution` (or null while pending), the `rawText` for the raw
 * disclosure, and the disclosure's open state (`rawOpen` + `onToggleRaw`,
 * owned by the live component). The live `PermissionCard` component computes
 * these once and calls here; tests call it directly with an injected
 * `scopeInfo`.
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
): m.Vnode {
  const title = permissionTitle(details, scopeInfo);
  const rawToggle = renderRawToggle(rawText, rawOpen, onToggleRaw);
  const rawBlock = renderRawBlock(rawText, rawOpen);

  if (resolution !== null) {
    // One compact line: the verdict reads inline right after the title, and
    // the raw toggle keeps to the right edge of the same row, so the receipt
    // never grows a second row.
    return m("div", { class: "permission-request" }, [
      renderEyebrow(),
      m("div", { class: "permission-request-receipt" }, [
        m("div", { class: "permission-request-badge permission-request-badge--sm" }, renderSubjectMark(details, 14)),
        m("div", { class: "permission-request-receipt-title" }, title ?? GENERIC_PERMISSION_TITLE),
        renderPermissionVerdict(resolution),
        rawToggle ? m("div", { class: "permission-request-receipt-toggle" }, rawToggle) : null,
      ]),
      rawBlock,
    ]);
  }

  if (details === null) {
    return m("div", { class: "permission-request" }, [
      renderEyebrow(),
      m(
        "div",
        { class: "permission-request-status" },
        hasResult ? "Couldn't read this request — see the Permissions tab." : "Waiting for the request to register…",
      ),
      rawToggle ? m("div", { class: "permission-request-toggle-row" }, rawToggle) : null,
      rawBlock,
    ]);
  }

  // The body must never be a bare badge: fall back to the generic subject only
  // when no rationale survived either, so a titled-but-redundant row never sits
  // under an identical eyebrow.
  const bodyTitle = title ?? (details.rationale === null ? GENERIC_PERMISSION_TITLE : null);
  return m("div", { class: "permission-request" }, [
    renderEyebrow(),
    m("div", { class: "permission-request-body" }, [
      m("div", { class: "permission-request-badge" }, renderSubjectMark(details, 16)),
      m("div", { class: "permission-request-info" }, [
        bodyTitle !== null ? m("div", { class: "permission-request-title" }, bodyTitle) : null,
        details.rationale ? m("div", { class: "permission-request-reason" }, details.rationale) : null,
      ]),
    ]),
    m("div", { class: "permission-request-actions" }, [
      m(
        "button",
        {
          class: "btn btn--primary",
          type: "button",
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
 */
export function PermissionCard(): m.Component<{
  toolCall: ToolCall;
  toolResult: ToolResultEvent | null;
  resolution: PermissionResolution | null;
}> {
  let rawOpen = false;
  return {
    view(vnode) {
      const { toolCall, toolResult, resolution } = vnode.attrs;
      const details = parsePermissionRequest(toolCall, toolResult);
      const scopeInfo = details?.scope ? getScopeInfo(details.scope) : null;
      const rawInput = toolCall.input_preview || "";
      const rawOutput = toolResult?.output || "";
      const rawText = rawOutput ? `${rawInput}\n\n${rawOutput}` : rawInput;
      // The transcript-classified resolution wins; before it lands, a verdict
      // the shell reported for this request (the user just resolved it in the
      // review popup) flips the card without waiting for the round trip.
      const effectiveResolution = resolution ?? (details ? shellPermissionResolutionFor(details.requestId) : null);
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
      );
    },
  };
}
