// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ToolCall, ToolResultEvent } from "../models/Response";
import type { SecretResolution } from "./message-classification";
import type { SecretCardHandlers, SecretCardState } from "./secret-card";
import {
  SECRET_STATUS_RETRY_DELAY_MS,
  isFiledSecretRequest,
  knownSecretResolution,
  noteSecretResolution,
  parseSecretRequest,
  renderSecretCard,
  resetSecretStatusCacheForTesting,
} from "./secret-card";

function makeToolCall(display?: "secret_request" | "permission_request"): ToolCall {
  return { tool_call_id: "call-1", tool_name: "Bash", input_chars: 120, ...(display ? { display } : {}) };
}

const FILED = {
  request_id: "secret-0123456789abcdef0123456789abcdef",
  chat_id: "agent-1",
  file: "svc",
  env_path: "data/.secrets/svc.env",
  variables: ["SVC_TOKEN", "SVC_URL"],
  rationale: "I need your widget API key to list the parts you asked about.",
  status: "pending",
  existing_variables: ["SVC_URL", "OTHER"],
  overwrites: ["SVC_URL"],
};

function makeResult(secretRequest: Record<string, unknown> | undefined, isError = false): ToolResultEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "tool_result",
    event_id: "evt-result-1",
    source: "session",
    tool_call_id: "call-1",
    tool_name: "Bash",
    output_chars: 300,
    is_error: isError,
    ...(secretRequest === undefined ? {} : { secret_request: secretRequest }),
  };
}

function idleState(overrides: Partial<SecretCardState> = {}): SecretCardState {
  return { valueByVariable: {}, note: "", isDeclining: false, isBusy: false, error: null, ...overrides };
}

function recordingHandlers(): SecretCardHandlers & { calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    onValueInput: (variable, value) => calls.push(`value:${variable}=${value}`),
    onNoteInput: (note) => calls.push(`note:${note}`),
    onSubmit: () => calls.push("submit"),
    onDeclineStart: () => calls.push("decline-start"),
    onDeclineCancel: () => calls.push("decline-cancel"),
    onDeclineConfirm: () => calls.push("decline-confirm"),
  };
}

function renderToDom(
  details: ReturnType<typeof parseSecretRequest>,
  resolution: SecretResolution | null,
  note: string | null,
  hasResult: boolean,
  state: SecretCardState,
  handlers: SecretCardHandlers,
): HTMLElement {
  const root = document.createElement("div");
  m.render(root, renderSecretCard(details, resolution, note, hasResult, state, handlers));
  return root;
}

afterEach(() => {
  resetSecretStatusCacheForTesting();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/** Let the fire-and-forget fetch chain inside knownSecretResolution settle. */
async function flushFetches(): Promise<void> {
  for (let hop = 0; hop < 6; hop += 1) {
    await Promise.resolve();
  }
}

describe("parseSecretRequest", () => {
  it("reads the filed request off the backend's structured field", () => {
    expect(parseSecretRequest(makeToolCall("secret_request"), makeResult(FILED))).toEqual({
      requestId: FILED.request_id,
      file: "svc",
      envPath: "data/.secrets/svc.env",
      variables: ["SVC_TOKEN", "SVC_URL"],
      overwrites: ["SVC_URL"],
      rationale: FILED.rationale,
    });
  });

  it("is null for a call that is not a secret request, an errored call, or an older backend's result", () => {
    expect(parseSecretRequest(makeToolCall("permission_request"), makeResult(FILED))).toBeNull();
    expect(parseSecretRequest(makeToolCall("secret_request"), makeResult(FILED, true))).toBeNull();
    expect(parseSecretRequest(makeToolCall("secret_request"), makeResult(undefined))).toBeNull();
  });

  it("counts a pending call as filed and a refused one as an ordinary tool call", () => {
    expect(isFiledSecretRequest(makeToolCall("secret_request"), null)).toBe(true);
    expect(isFiledSecretRequest(makeToolCall("secret_request"), makeResult(FILED))).toBe(true);
    expect(isFiledSecretRequest(makeToolCall("secret_request"), makeResult(undefined, true))).toBe(false);
    expect(isFiledSecretRequest(makeToolCall(), makeResult(FILED))).toBe(false);
  });
});

describe("renderSecretCard", () => {
  const details = parseSecretRequest(makeToolCall("secret_request"), makeResult(FILED));

  it("renders one password input per variable, the rationale, the file, and the overwrite note while pending", () => {
    const handlers = recordingHandlers();
    const root = renderToDom(details, null, null, true, idleState(), handlers);
    const inputs = [...root.querySelectorAll<HTMLInputElement>(".secret-request-inputs input")];
    expect(inputs.map((input) => input.type)).toEqual(["password", "password"]);
    expect(inputs.map((input) => input.dataset.variable)).toEqual(["SVC_TOKEN", "SVC_URL"]);
    expect(root.querySelector(".secret-request-reason")?.textContent).toBe(FILED.rationale);
    expect(root.querySelector(".secret-request-title")?.textContent).toBe("Store data/.secrets/svc.env");
    expect(root.querySelector(".secret-request-overwrites")?.textContent).toBe("Replaces the existing SVC_URL.");
    const [submit, decline] = [...root.querySelectorAll<HTMLButtonElement>(".secret-request-actions button")];
    expect(submit.textContent).toBe("Submit");
    expect(submit.disabled).toBe(true);
    decline.click();
    expect(handlers.calls).toEqual(["decline-start"]);
  });

  it("enables Submit once every variable is filled and routes the click to the handler", () => {
    const handlers = recordingHandlers();
    const state = idleState({ valueByVariable: { SVC_TOKEN: "t", SVC_URL: "u" } });
    const root = renderToDom(details, null, null, true, state, handlers);
    const submit = root.querySelector<HTMLButtonElement>(".secret-request-actions button")!;
    expect(submit.disabled).toBe(false);
    submit.click();
    expect(handlers.calls).toEqual(["submit"]);
    // Typing goes through the handler, never into the DOM's own memory of the value.
    const input = root.querySelector<HTMLInputElement>("input[data-variable='SVC_TOKEN']")!;
    input.value = "typed";
    input.dispatchEvent(new Event("input"));
    expect(handlers.calls).toEqual(["submit", "value:SVC_TOKEN=typed"]);
  });

  it("shows the decline note field with its confirm and back buttons while declining", () => {
    const handlers = recordingHandlers();
    const root = renderToDom(details, null, null, true, idleState({ isDeclining: true }), handlers);
    expect(root.querySelector(".secret-request-inputs")).toBeNull();
    expect(root.querySelector(".secret-request-actions")).toBeNull();
    const [confirm, back] = [...root.querySelectorAll<HTMLButtonElement>(".secret-request-decline button")];
    confirm.click();
    back.click();
    expect(handlers.calls).toEqual(["decline-confirm", "decline-cancel"]);
  });

  it("shows the last error under the inputs", () => {
    const root = renderToDom(
      details,
      null,
      null,
      true,
      idleState({ error: "The chat app could not be reached." }),
      recordingHandlers(),
    );
    expect(root.querySelector(".secret-request-error")?.textContent).toBe("The chat app could not be reached.");
  });

  it.each([
    ["stored", "Stored"],
    ["declined", "Declined"],
    ["superseded", "Replaced by a newer request"],
  ] as const)("renders a resolved card as a receipt with the %s verdict and no inputs", (resolution, label) => {
    const root = renderToDom(details, resolution, null, true, idleState(), recordingHandlers());
    expect(root.querySelector(".secret-request-inputs")).toBeNull();
    expect(root.querySelector("button")).toBeNull();
    expect(root.querySelector(".secret-request-receipt-title")?.textContent).toBe("data/.secrets/svc.env");
    expect(root.querySelector(`.secret-request-verdict--${resolution}`)?.textContent).toBe(label);
  });

  it("shows the user's note on a declined card", () => {
    const root = renderToDom(details, "declined", "use the other account", true, idleState(), recordingHandlers());
    expect(root.querySelector(".secret-request-note")?.textContent).toBe("use the other account");
  });

  it("waits while the request has no result yet, and says so honestly when the result is unreadable", () => {
    const waiting = renderToDom(null, null, null, false, idleState(), recordingHandlers());
    expect(waiting.querySelector(".secret-request-status")?.textContent).toContain("Waiting");
    expect(waiting.querySelector("input")).toBeNull();
    const unreadable = renderToDom(null, null, null, true, idleState(), recordingHandlers());
    expect(unreadable.querySelector(".secret-request-status")?.textContent).toContain("Couldn't read this request");
    expect(unreadable.querySelector("button")).toBeNull();
  });
});

describe("status hydration", () => {
  it("asks the backend once for an unknown request and caches a definitive answer", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ status: "stored" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    expect(knownSecretResolution("secret-1")).toBeNull();
    await vi.waitFor(() => expect(knownSecretResolution("secret-1")).toBe("stored"));
    knownSecretResolution("secret-1");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [requestedUrl] = fetchMock.mock.calls[0] as unknown[];
    expect(String(requestedUrl)).toContain("/api/secret-requests/secret-1");
  });

  it("takes a verdict this page produced itself without a fetch", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    noteSecretResolution("secret-2", "declined");
    expect(knownSecretResolution("secret-2")).toBe("declined");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not refetch a failed lookup on the next render, only once the retry delay passes", async () => {
    // The failure's own redraw is the next render: without a delay the card would
    // fetch back to back for as long as the chat app answers 5xx.
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("", { status: 502 }))
      .mockResolvedValue(new Response(JSON.stringify({ status: "stored" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);

    expect(knownSecretResolution("secret-3")).toBeNull();
    await flushFetches();
    expect(knownSecretResolution("secret-3")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(SECRET_STATUS_RETRY_DELAY_MS);
    expect(knownSecretResolution("secret-3")).toBeNull();
    await flushFetches();
    expect(knownSecretResolution("secret-3")).toBe("stored");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
