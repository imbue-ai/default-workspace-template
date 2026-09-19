/**
 * The secret card: an agent asked the user for a credential through the
 * connect-external-service skill's `request_secret.py`, and the transcript renders
 * that tool call as a card with one password input per variable rather than a
 * "Tool: Bash" block. The values go straight from the inputs to the chat app
 * backend, which writes `data/.secrets/<file>.env` and tells the agent the file
 * and the variable names; nothing here keeps a value once the request resolves.
 *
 * Same seam as the permission card: `SecretCard` is the live component (it parses
 * the request, owns the inputs and the submit/decline requests, and hydrates a
 * request's status from the backend after a reload), and `renderSecretCard` is the
 * pure renderer it delegates to, which the tests drive directly.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import type { ToolCall, ToolResultEvent } from "../models/Response";
import type { SecretResolution } from "./message-classification";
import { isSecretRequestCall } from "./message-classification";

/** The fields the request script echoes, parsed off the backend's structured
 *  `secret_request` field. */
export interface SecretRequestDetails {
  requestId: string;
  /** The `<file>` of `data/.secrets/<file>.env`, and the path itself. */
  file: string;
  envPath: string;
  variables: string[];
  /** Variables the file already holds that this request will overwrite. */
  overwrites: string[];
  rationale: string | null;
}

function asObject(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

/** The one place that knows the echoed object's field names. */
function detailsFromResponseObject(obj: Record<string, unknown>): SecretRequestDetails | null {
  if (typeof obj.request_id !== "string" || typeof obj.file !== "string") return null;
  return {
    requestId: obj.request_id,
    file: obj.file,
    envPath: typeof obj.env_path === "string" ? obj.env_path : `data/.secrets/${obj.file}.env`,
    variables: stringList(obj.variables),
    overwrites: stringList(obj.overwrites),
    rationale: typeof obj.rationale === "string" ? obj.rationale : null,
  };
}

/** Parse a filed secret request out of its tool result, or null when the call is
 *  not a secret request, errored, or carries no structured field (a transcript
 *  from an older backend), in which case the card shows its can't-read state. */
export function parseSecretRequest(
  toolCall: ToolCall,
  toolResult: ToolResultEvent | null,
): SecretRequestDetails | null {
  if (!isSecretRequestCall(toolCall)) return null;
  if (!toolResult || toolResult.is_error === true) return null;
  const structured = asObject(toolResult.secret_request);
  return structured === null ? null : detailsFromResponseObject(structured);
}

/** Whether this tool call is a secret request the chat app actually FILED (or one
 *  still in flight, which counts: that is when the user most needs to see it). A
 *  call the harness refused or the script failed produced no request and renders
 *  as the ordinary tool call it is. */
export function isFiledSecretRequest(toolCall: ToolCall, toolResult: ToolResultEvent | null): boolean {
  if (!isSecretRequestCall(toolCall)) return false;
  if (toolResult === null) return true;
  return parseSecretRequest(toolCall, toolResult) !== null;
}

// Status hydration
//
// The transcript's resolution notice is the primary signal, and a submit from this
// page flips the card at once. A page rebuilt after either has neither, so the
// card asks the backend once for the request's status; only a definitive answer
// is cached, so a transient failure is asked again on the next render.

type StatusEntry = { state: "loading" } | { state: "ready"; resolution: SecretResolution | null };

const statusCache = new Map<string, StatusEntry>();

export function knownSecretResolution(requestId: string): SecretResolution | null {
  const cached = statusCache.get(requestId);
  if (cached === undefined) {
    statusCache.set(requestId, { state: "loading" });
    void hydrateStatus(requestId);
    return null;
  }
  return cached.state === "ready" ? cached.resolution : null;
}

function resolutionFromStatus(status: unknown): SecretResolution | null {
  return status === "stored" || status === "declined" || status === "superseded" ? status : null;
}

async function hydrateStatus(requestId: string): Promise<void> {
  try {
    const response = await fetch(apiUrl(`/api/secret-requests/${encodeURIComponent(requestId)}`));
    if (response.ok) {
      const body = (await response.json()) as { status?: unknown };
      statusCache.set(requestId, { state: "ready", resolution: resolutionFromStatus(body.status) });
    } else if (response.status === 404) {
      statusCache.set(requestId, { state: "ready", resolution: null });
    } else {
      statusCache.delete(requestId);
    }
  } catch {
    statusCache.delete(requestId);
  }
  m.redraw();
}

/** Record a verdict this page produced itself (a submit or decline), so the card
 *  flips before the notice's transcript round trip. */
export function noteSecretResolution(requestId: string, resolution: SecretResolution): void {
  statusCache.set(requestId, { state: "ready", resolution });
}

/** Drop the cache so the next test starts from a quiet page. */
export function resetSecretStatusCacheForTesting(): void {
  statusCache.clear();
}

// Submit and decline

async function postJson(path: string, body: unknown): Promise<{ ok: boolean; detail: string }> {
  try {
    const response = await fetch(apiUrl(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (response.ok) return { ok: true, detail: "" };
    let detail = `The chat app answered HTTP ${response.status}.`;
    try {
      const parsed = (await response.json()) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      // A non-JSON error body keeps the status-code wording.
    }
    return { ok: false, detail };
  } catch {
    return { ok: false, detail: "The chat app could not be reached." };
  }
}

export function submitSecret(
  requestId: string,
  values: Record<string, string>,
): Promise<{ ok: boolean; detail: string }> {
  return postJson(`/api/secret-requests/${encodeURIComponent(requestId)}/submit`, { values });
}

export function declineSecret(requestId: string, note: string): Promise<{ ok: boolean; detail: string }> {
  return postJson(`/api/secret-requests/${encodeURIComponent(requestId)}/decline`, note ? { note } : {});
}

// Rendering

/** The card's own state beyond the request: what the inputs hold and what the
 *  last submit or decline said. Owned by the live component. */
export interface SecretCardState {
  valueByVariable: Record<string, string>;
  note: string;
  isDeclining: boolean;
  isBusy: boolean;
  error: string | null;
}

export interface SecretCardHandlers {
  onValueInput: (variable: string, value: string) => void;
  onNoteInput: (note: string) => void;
  onSubmit: () => void;
  onDeclineStart: () => void;
  onDeclineCancel: () => void;
  onDeclineConfirm: () => void;
}

const CARD_CLASS = "secret-request max-w-[560px] rounded-lg border bg-composer px-3.5 py-3";

const VERDICT_COLOR: Record<SecretResolution, string> = {
  stored: "text-accent",
  declined: "text-secondary",
  superseded: "text-faint",
};

function verdictLabel(resolution: SecretResolution): string {
  if (resolution === "stored") return "Stored";
  if (resolution === "declined") return "Declined";
  return "Replaced by a newer request";
}

function renderEyebrow(): m.Vnode {
  return m(
    "div",
    { class: "secret-request-eyebrow flex items-center gap-1.5 text-(length:--font-size-helper) text-secondary" },
    [m.trust(icon("key", { size: 13, className: "secret-request-icon shrink-0" })), m("span", "Secret request")],
  );
}

function renderVerdict(resolution: SecretResolution): m.Vnode {
  const name = resolution === "stored" ? "check" : resolution === "declined" ? "close" : "alert";
  return m(
    "div",
    {
      class:
        `secret-request-verdict secret-request-verdict--${resolution} ` +
        `inline-flex shrink-0 items-center gap-[5px] text-(length:--font-size-helper) font-semibold ` +
        VERDICT_COLOR[resolution],
    },
    [
      m.trust(icon(name, { size: 13, className: "secret-request-verdict-icon shrink-0" })),
      m("span", verdictLabel(resolution)),
    ],
  );
}

function renderTitle(details: SecretRequestDetails): m.Vnode {
  return m("div", { class: "secret-request-title type-label text-primary" }, `Store ${details.envPath}`);
}

function renderInputs(details: SecretRequestDetails, state: SecretCardState, handlers: SecretCardHandlers): m.Vnode {
  return m(
    "div",
    { class: "secret-request-inputs mt-2.5 flex flex-col gap-2" },
    details.variables.map((variable) =>
      m("label", { class: "flex flex-col gap-1 text-(length:--font-size-helper) text-secondary" }, [
        m("span", { class: "secret-request-variable font-mono" }, variable),
        m("input", {
          class: inputClass({ mono: true }),
          type: "password",
          value: state.valueByVariable[variable] ?? "",
          spellcheck: false,
          autocomplete: "off",
          "data-1p-ignore": "",
          "data-variable": variable,
          disabled: state.isBusy,
          oninput: (event: Event) => handlers.onValueInput(variable, (event.target as HTMLInputElement).value),
        }),
      ]),
    ),
  );
}

function renderDeclineForm(state: SecretCardState, handlers: SecretCardHandlers): m.Vnode {
  return m("div", { class: "secret-request-decline mt-2.5 flex flex-col gap-2" }, [
    m("input", {
      class: inputClass(),
      type: "text",
      value: state.note,
      placeholder: "Optional note for the agent",
      disabled: state.isBusy,
      "data-e2e": "secret-decline-note",
      oninput: (event: Event) => handlers.onNoteInput((event.target as HTMLInputElement).value),
    }),
    m("div", { class: "flex items-center gap-2" }, [
      m(
        Button,
        { variant: "secondary", disabled: state.isBusy, onclick: () => handlers.onDeclineConfirm() },
        "Decline",
      ),
      m(Button, { variant: "ghost", disabled: state.isBusy, onclick: () => handlers.onDeclineCancel() }, "Back"),
    ]),
  ]);
}

/**
 * Pure renderer. Four states under one eyebrow:
 *   - Resolved (`resolution` non-null): a one-line receipt with the verdict, and the
 *     decline note when the transcript carried one.
 *   - Pending and parsed: the rationale as the agent's claim, the target file, a note
 *     naming the variables a submit overwrites, the password inputs, Submit and Decline.
 *   - Pending and unparsed: waiting for the request to register, or an honest can't-read
 *     line for a transcript recorded by an older backend.
 */
export function renderSecretCard(
  details: SecretRequestDetails | null,
  resolution: SecretResolution | null,
  note: string | null,
  hasResult: boolean,
  state: SecretCardState,
  handlers: SecretCardHandlers,
): m.Vnode {
  if (resolution !== null) {
    return m("div", { class: CARD_CLASS }, [
      renderEyebrow(),
      m("div", { class: "secret-request-receipt mt-2.5 flex items-center gap-2" }, [
        m(
          "div",
          { class: "secret-request-receipt-title min-w-0 truncate type-label text-primary" },
          details ? details.envPath : "Secret request",
        ),
        renderVerdict(resolution),
      ]),
      note
        ? m("div", { class: "secret-request-note mt-1.5 text-(length:--font-size-helper) text-secondary" }, note)
        : null,
    ]);
  }

  if (details === null) {
    return m("div", { class: CARD_CLASS }, [
      renderEyebrow(),
      m(
        "div",
        { class: "secret-request-status mt-[9px] text-(length:--font-size-body) text-secondary" },
        hasResult
          ? "Couldn't read this request. Ask the agent to request it again."
          : "Waiting for the request to register…",
      ),
    ]);
  }

  const canSubmit =
    !state.isBusy && details.variables.every((variable) => (state.valueByVariable[variable] ?? "") !== "");
  return m("div", { class: CARD_CLASS }, [
    renderEyebrow(),
    m("div", { class: "secret-request-body mt-2.5 flex flex-col gap-1" }, [
      renderTitle(details),
      details.rationale
        ? m(
            "div",
            { class: "secret-request-reason text-(length:--font-size-helper) leading-[1.45] text-secondary" },
            details.rationale,
          )
        : null,
      details.overwrites.length > 0
        ? m(
            "div",
            { class: "secret-request-overwrites text-(length:--font-size-helper) text-warning" },
            `Replaces the existing ${details.overwrites.join(", ")}.`,
          )
        : null,
      m(
        "div",
        { class: "secret-request-privacy text-(length:--font-size-helper) text-faint" },
        "Stored on this workspace's disk for its programs to use; the agent never sees the values.",
      ),
    ]),
    state.isDeclining ? renderDeclineForm(state, handlers) : renderInputs(details, state, handlers),
    state.error
      ? m("div", { class: "secret-request-error mt-2 text-(length:--font-size-helper) text-danger" }, state.error)
      : null,
    state.isDeclining
      ? null
      : m("div", { class: "secret-request-actions mt-3 flex items-center gap-2.5" }, [
          m(Button, { variant: "primary", disabled: !canSubmit, onclick: () => handlers.onSubmit() }, "Submit"),
          m(Button, { variant: "ghost", disabled: state.isBusy, onclick: () => handlers.onDeclineStart() }, "Decline"),
        ]),
  ]);
}

/** The live card: parses the request once per render, owns the inputs and the
 *  in-flight submit or decline, and flips itself the moment the backend answers. */
export function SecretCard(): m.Component<{
  toolCall: ToolCall;
  toolResult: ToolResultEvent | null;
  /** The transcript's verdict for this request, or null while it is pending. */
  resolution: SecretResolution | null;
  /** The decline note the transcript carried, when it did. */
  note: string | null;
}> {
  const state: SecretCardState = { valueByVariable: {}, note: "", isDeclining: false, isBusy: false, error: null };

  async function run(
    requestId: string,
    action: () => Promise<{ ok: boolean; detail: string }>,
    verdict: SecretResolution,
  ): Promise<void> {
    state.isBusy = true;
    state.error = null;
    m.redraw();
    const outcome = await action();
    state.isBusy = false;
    if (outcome.ok) {
      // The values are done with: drop them the moment the backend has them.
      state.valueByVariable = {};
      noteSecretResolution(requestId, verdict);
    } else {
      state.error = outcome.detail;
    }
    m.redraw();
  }

  return {
    view(vnode) {
      const { toolCall, toolResult, resolution, note } = vnode.attrs;
      const details = parseSecretRequest(toolCall, toolResult);
      const effectiveResolution = resolution ?? (details ? knownSecretResolution(details.requestId) : null);
      const handlers: SecretCardHandlers = {
        onValueInput: (variable, value) => {
          state.valueByVariable[variable] = value;
        },
        onNoteInput: (value) => {
          state.note = value;
        },
        onSubmit: () => {
          if (details === null) return;
          void run(details.requestId, () => submitSecret(details.requestId, { ...state.valueByVariable }), "stored");
        },
        onDeclineStart: () => {
          state.isDeclining = true;
        },
        onDeclineCancel: () => {
          state.isDeclining = false;
        },
        onDeclineConfirm: () => {
          if (details === null) return;
          void run(details.requestId, () => declineSecret(details.requestId, state.note.trim()), "declined");
        },
      };
      return renderSecretCard(details, effectiveResolution, note, toolResult !== null, state, handlers);
    },
  };
}
