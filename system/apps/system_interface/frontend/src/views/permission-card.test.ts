import m from "mithril";
import { describe, expect, it, vi } from "vitest";
import { resetEmbedEndpointForTesting } from "../embed";
import type { ToolCall, ToolResultEvent } from "../models/Response";
import type { ScopeInfo } from "./latchkey-scope-info";
import type { PermissionResolution } from "./message-classification";
import {
  PermissionCard,
  initShellPermissionResolutions,
  openPermissionRequest,
  parsePermissionRequest,
  renderPermissionCard,
  shellPermissionResolutionFor,
} from "./permission-card";

/** Stand in a `window` whose parent is the embedding minds chrome, with the
 *  embed endpoint rebound to it, for the duration of `run`. `run` receives a
 *  deliver function that plays a message event into that window. */
function withStubbedEmbedder(parent: object, run: (deliver: (event: unknown) => void) => void): void {
  const listeners: ((event: unknown) => void)[] = [];
  resetEmbedEndpointForTesting();
  vi.stubGlobal("window", {
    parent,
    addEventListener: (type: string, handler: (event: unknown) => void) => {
      if (type === "message") listeners.push(handler);
    },
    removeEventListener: () => undefined,
  });
  try {
    run((event) => {
      for (const listener of [...listeners]) listener(event);
    });
  } finally {
    vi.unstubAllGlobals();
    resetEmbedEndpointForTesting();
  }
}

/** Play one embedder->workspace message into the subscribed cards, through the
 *  real contract endpoint -- so the source and payload checks the shell's
 *  messages pass through are the ones exercised here. */
function deliverFromEmbedder(data: Record<string, unknown>, options: { isFromEmbedder?: boolean } = {}): void {
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  const parent = {};
  withStubbedEmbedder(parent, (deliver) => {
    initShellPermissionResolutions();
    deliver({
      // A nested third-party iframe can post here but is not `window.parent`.
      source: options.isFromEmbedder === false ? {} : parent,
      origin: "https://host-ab12.localhost",
      data,
    });
  });
  vi.restoreAllMocks();
}

function makeToolCall(inputPreview: string): ToolCall {
  return {
    tool_call_id: "call-1",
    tool_name: "Bash",
    input_preview: inputPreview,
  };
}

function makeResult(output: string, isError = false): ToolResultEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "tool_result",
    event_id: "evt-result-1",
    source: "session",
    message_uuid: "uuid-1",
    tool_call_id: "call-1",
    tool_name: "Bash",
    output,
    is_error: isError,
  };
}

// Mirror the `PermissionCard` component's pre-render work so each test exercises
// the pure renderer exactly as the live card calls it: parse the request once,
// assemble the raw-request text, and pass in an injected `scopeInfo` (instead of
// driving the async gateway lookup) plus the raw-disclosure state the live
// component owns.
function renderCardFor(
  toolCall: ToolCall,
  toolResult: ToolResultEvent | null,
  resolution: PermissionResolution | null = null,
  scopeInfo: ScopeInfo | null = null,
  rawOpen = false,
  onToggleRaw: () => void = () => {},
): m.Vnode {
  const details = parsePermissionRequest(toolCall, toolResult);
  const rawInput = toolCall.input_preview || "";
  const rawOutput = toolResult?.output || "";
  const rawText = rawOutput ? `${rawInput}\n\n${rawOutput}` : rawInput;
  return renderPermissionCard(details, scopeInfo, resolution, rawText, toolResult !== null, rawOpen, onToggleRaw);
}

// A realistic input_preview: the command is JSON-encoded and may be truncated
// at 200 chars, but the reserved host appears near the start.
const PERMISSION_INPUT = JSON.stringify({
  command:
    "latchkey curl -XPOST http://latchkey-self.invalid/permission-requests \\\n  -H 'Content-Type: application/json' \\\n  -d '{...}'",
});

// A realistic output: curl writes a progress meter to stderr/stdout before the
// JSON body, so the whole thing is not directly JSON-parseable. The body
// carries the rich fields the card surfaces (rationale, request_type, payload).
const PERMISSION_OUTPUT = `  % Total    % Received % Xferd
100  1007  100   670  100   337
{
  "request_id": "885711ec07bf47239d71294e1534330b",
  "agent_id": "agent-28dc23edadd34caeaba58441ac8e7218",
  "rationale": "I need to read #eng-releases to summarize the deploy thread.",
  "request_type": "predefined",
  "payload": { "scope": "slack-api", "permissions": ["slack-read-all"] }
}`;

// A file-sharing request: payload carries a path and access mode instead.
const FILE_SHARING_OUTPUT = `{"request_id":"fs-1","rationale":"write the report locally","request_type":"file-sharing","payload":{"path":"/Users/you/Documents/report","access":"WRITE"}}`;

// A workspace request (acting on the user's other Minds workspaces): payload
// carries verb names and a target workspace, neither of which the card renders
// as details for now -- only the heading and the button.
const WORKSPACE_OUTPUT = `{"request_id":"ws-1","rationale":"export a backup of the old workspace","request_type":"workspace","payload":{"permissions":["minds-workspaces-backups-export"],"target_workspace_id":"agent-a3b7b469ee8341779c9ede1a798c447f"}}`;

// An accounts request (listing the device's signed-in accounts).
const ACCOUNTS_OUTPUT = `{"request_id":"acct-1","rationale":"check which account is signed in","request_type":"accounts","payload":{}}`;

describe("parsePermissionRequest", () => {
  it("parses the rich details of a successful predefined creation POST", () => {
    const result = parsePermissionRequest(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT));
    expect(result).toEqual({
      requestId: "885711ec07bf47239d71294e1534330b",
      requestType: "predefined",
      rationale: "I need to read #eng-releases to summarize the deploy thread.",
      scope: "slack-api",
      permissions: ["slack-read-all"],
      path: null,
      access: null,
    });
  });

  it("parses a file-sharing request's path and access mode", () => {
    const result = parsePermissionRequest(makeToolCall(PERMISSION_INPUT), makeResult(FILE_SHARING_OUTPUT));
    expect(result).toMatchObject({
      requestId: "fs-1",
      requestType: "file-sharing",
      path: "/Users/you/Documents/report",
      access: "WRITE",
      scope: null,
    });
  });

  it("parses a workspace request's id and type", () => {
    const result = parsePermissionRequest(makeToolCall(PERMISSION_INPUT), makeResult(WORKSPACE_OUTPUT));
    expect(result).toMatchObject({
      requestId: "ws-1",
      requestType: "workspace",
      rationale: "export a backup of the old workspace",
      scope: null,
      path: null,
    });
  });

  it("ignores tool calls that are not permission-request POSTs", () => {
    const unrelated = makeToolCall(JSON.stringify({ command: "ls -la" }));
    expect(parsePermissionRequest(unrelated, makeResult("anything"))).toBeNull();
  });

  it("ignores reads of the latchkey permissions endpoints (non-POST host)", () => {
    const read = makeToolCall(
      JSON.stringify({ command: "latchkey curl http://latchkey-self.invalid/permissions/self" }),
    );
    expect(parsePermissionRequest(read, makeResult('{"rules": []}'))).toBeNull();
  });

  it("returns null while the tool result is still pending", () => {
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT), null)).toBeNull();
  });

  it("returns null when the creation call errored", () => {
    const errored = makeResult("request not permitted by the user", true);
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT), errored)).toBeNull();
  });

  it("returns null when the output has no JSON body", () => {
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT), makeResult("nope"))).toBeNull();
  });

  it("returns null when the JSON body has no request_id", () => {
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT), makeResult('{"agent_id":"a"}'))).toBeNull();
  });
});

// Depth-first search for the first vnode matching a predicate.
function findVnode(
  node: unknown,
  pred: (v: { tag?: unknown }) => boolean,
): { tag?: unknown; children?: unknown } | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const hit = findVnode(child, pred);
      if (hit) return hit;
    }
    return null;
  }
  if (node !== null && typeof node === "object") {
    const vnode = node as { tag?: unknown; children?: unknown };
    if (pred(vnode)) return vnode;
    return findVnode(vnode.children, pred);
  }
  return null;
}

// The exact text of the first text vnode (tag "#") under a node, or null.
function textOf(node: unknown): string | null {
  const t = findVnode(node, (v) => v.tag === "#" && typeof (v as { children?: unknown }).children === "string");
  return t ? (t.children as string) : null;
}

// The first vnode carrying `className` (exact word match within a possibly
// multi-class attribute), or null.
function findByClass(
  node: unknown,
  className: string,
): { attrs?: Record<string, unknown>; children?: unknown } | null {
  return findVnode(node, (v) => {
    const cls = (v as { attrs?: { className?: unknown } }).attrs?.className;
    return typeof cls === "string" && cls.split(" ").includes(className);
  }) as { attrs?: Record<string, unknown>; children?: unknown } | null;
}

// The solid "Review & respond" button (distinct from the raw-disclosure toggle,
// which is also a <button>).
function findReviewButton(node: unknown): { attrs?: Record<string, unknown>; children?: unknown } | null {
  return findByClass(node, "permission-request-button");
}

describe("renderPermissionCard", () => {
  it("shows the eyebrow, title, rationale, and review button on a pending card", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT));

    // The eyebrow reads "Permission request"; the title carries the specific
    // subject (the raw scope until the gateway catalog resolves a name).
    expect(textOf(findByClass(vnode, "permission-request-eyebrow"))).toBe("Permission request");
    expect(textOf(findByClass(vnode, "permission-request-title"))).toBe("slack-api");

    // The agent's reason renders directly beneath the title, with no label.
    expect(textOf(findByClass(vnode, "permission-request-reason"))).toBe(
      "I need to read #eng-releases to summarize the deploy thread.",
    );

    // The card no longer lists the specific permissions -- those live in the
    // review modal and the raw disclosure.
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "slack-read-all"),
    ).toBeNull();

    const button = findReviewButton(vnode);
    expect(button).not.toBeNull();
    expect(textOf(button)).toBe("Review & respond");
  });

  it("wires the button to open the modal with the request id", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT));
    const button = findReviewButton(vnode) as { attrs?: { onclick?: (e: Event) => void } } | null;

    const postMessage = vi.fn();
    withStubbedEmbedder({ postMessage }, () =>
      button?.attrs?.onclick?.({ preventDefault() {}, stopPropagation() {} } as unknown as Event),
    );
    expect(postMessage).toHaveBeenCalledWith(
      { type: "minds:open-request-modal", requestId: "885711ec07bf47239d71294e1534330b" },
      "*",
    );
  });

  it("shows a pending state with no review button before the result arrives", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), null);

    expect(textOf(findByClass(vnode, "permission-request-eyebrow"))).toBe("Permission request");
    expect(findReviewButton(vnode)).toBeNull();
    expect(textOf(findByClass(vnode, "permission-request-status"))).toBe("Waiting for the request to register\u2026");
  });

  it("says the request couldn't be read (not 'waiting') when a result arrived but has no request id", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult('{"agent_id":"a"}'));

    expect(findReviewButton(vnode)).toBeNull();
    const text = textOf(findByClass(vnode, "permission-request-status"));
    expect(text).not.toBe("Waiting for the request to register\u2026");
    expect(text).toContain("Couldn't read this request");
  });

  it("titles a file-sharing request 'Local files'", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(FILE_SHARING_OUTPUT));
    expect(textOf(findByClass(vnode, "permission-request-title"))).toBe("Local files");
  });

  it("titles an accounts request 'Device accounts'", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(ACCOUNTS_OUTPUT));
    expect(textOf(findByClass(vnode, "permission-request-title"))).toBe("Device accounts");
  });

  it("titles a workspace request 'Other machines' with a button and no permission specifics", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(WORKSPACE_OUTPUT));

    expect(textOf(findByClass(vnode, "permission-request-title"))).toBe("Other machines");

    // Neither the verb names nor the target workspace id render on the card.
    expect(
      findVnode(
        vnode,
        (v) => v.tag === "#" && (v as { children?: unknown }).children === "minds-workspaces-backups-export",
      ),
    ).toBeNull();
    expect(
      findVnode(
        vnode,
        (v) => v.tag === "#" && (v as { children?: unknown }).children === "agent-a3b7b469ee8341779c9ede1a798c447f",
      ),
    ).toBeNull();

    // The rationale still shows, and the button opens the modal by request id.
    expect(textOf(findByClass(vnode, "permission-request-reason"))).toBe("export a backup of the old workspace");
    const button = findReviewButton(vnode);
    expect(button).not.toBeNull();
    expect(textOf(button)).toBe("Review & respond");
  });

  it("shows an Approved receipt and no review button once granted", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT), "granted");
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "Approved"),
    ).not.toBeNull();
    // The compact receipt still names what was requested.
    expect(textOf(findByClass(vnode, "permission-request-receipt-title"))).toBe("slack-api");
    // The action button is replaced by the verdict receipt.
    expect(findReviewButton(vnode)).toBeNull();
  });

  it("shows a Denied receipt once denied", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT), "denied");
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "Denied"),
    ).not.toBeNull();
    expect(findReviewButton(vnode)).toBeNull();
  });

  it("shows a couldn't-complete receipt for an error outcome", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT), "error");
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "Couldn't complete"),
    ).not.toBeNull();
    expect(findReviewButton(vnode)).toBeNull();
  });

  it("uses the gateway service name as the title once the catalog resolves", () => {
    const scopeInfo: ScopeInfo = {
      scope: "slack-api",
      display_name: "Slack",
      description: "Any interaction with the Slack API.",
      permissions: [{ name: "slack-read-all", description: "All read operations across the Slack API." }],
    };
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT), null, scopeInfo);
    expect(textOf(findByClass(vnode, "permission-request-title"))).toBe("Slack");
  });

  it("offers a closed raw disclosure whose toggle reports the click", () => {
    const onToggleRaw = vi.fn();
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT),
      makeResult(PERMISSION_OUTPUT),
      null,
      null,
      false,
      onToggleRaw,
    );

    const toggle = findByClass(vnode, "permission-request-raw-toggle") as {
      attrs?: { onclick?: (e: Event) => void };
    } | null;
    expect(toggle).not.toBeNull();
    expect(textOf(toggle)).toBe("Show raw request");
    // Closed: the raw pre isn't rendered.
    expect(findVnode(vnode, (v) => v.tag === "pre")).toBeNull();

    toggle?.attrs?.onclick?.({ preventDefault() {}, stopPropagation() {} } as unknown as Event);
    expect(onToggleRaw).toHaveBeenCalledTimes(1);
  });

  it("renders the raw request text and a 'Hide' toggle when the disclosure is open", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT), null, null, true);

    expect(textOf(findByClass(vnode, "permission-request-raw-toggle"))).toBe("Hide raw request");
    const raw = findByClass(vnode, "permission-request-raw");
    expect(raw).not.toBeNull();
    const rawTextNode = findVnode(
      raw,
      (v) => v.tag === "#" && typeof (v as { children?: unknown }).children === "string",
    );
    expect(rawTextNode?.children as string).toContain('"request_id": "885711ec07bf47239d71294e1534330b"');
  });

  it("keeps the raw disclosure available on a resolved card", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT), makeResult(PERMISSION_OUTPUT), "granted", null, true);
    expect(textOf(findByClass(vnode, "permission-request-raw-toggle"))).toBe("Hide raw request");
    expect(findByClass(vnode, "permission-request-raw")).not.toBeNull();
  });
});

describe("openPermissionRequest", () => {
  it("posts the open-request-modal message to the parent window", () => {
    // The chat UI runs inside an iframe; vitest's node environment has no
    // `window`, so stand one in with a spy parent. The embed endpoint binds to
    // whatever window exists on first use, so reset it around the stub.
    const postMessage = vi.fn();
    withStubbedEmbedder({ postMessage }, () => openPermissionRequest("req-123"));
    expect(postMessage).toHaveBeenCalledWith({ type: "minds:open-request-modal", requestId: "req-123" }, "*");
  });
});

describe("shell permission resolutions", () => {
  it("rejects an off-shape payload without recording anything", () => {
    // Delivered through the real contract, so this covers the validation the
    // shell's messages actually pass through -- not a second copy of it.
    deliverFromEmbedder({
      type: "minds:permission-request-resolved",
      requestId: "req-rejected",
      resolution: "error",
    });
    deliverFromEmbedder({ type: "minds:permission-request-resolved", requestId: "req-rejected" });
    deliverFromEmbedder({ type: "minds:permission-request-resolved", resolution: "granted" });
    expect(shellPermissionResolutionFor("req-rejected")).toBeNull();

    // The same delivery with a verdict the contract accepts does record, so
    // the rejections above are the payloads' doing and not a dead harness.
    deliverFromEmbedder({
      type: "minds:permission-request-resolved",
      requestId: "req-rejected",
      resolution: "granted",
    });
    expect(shellPermissionResolutionFor("req-rejected")).toBe("granted");
  });

  it("ignores a resolution from anyone but this page's embedder", () => {
    deliverFromEmbedder(
      { type: "minds:permission-request-resolved", requestId: "req-nested", resolution: "granted" },
      { isFromEmbedder: false },
    );
    expect(shellPermissionResolutionFor("req-nested")).toBeNull();
  });

  it("records the verdict the shell reports", () => {
    deliverFromEmbedder({
      type: "minds:permission-request-resolved",
      requestId: "req-recorded",
      resolution: "denied",
    });
    expect(shellPermissionResolutionFor("req-recorded")).toBe("denied");
  });

  it("flips the live card to the shell's verdict before the transcript resolution lands", () => {
    // The shell (the Minds review popup) reported this request granted; the
    // transcript walk hasn't classified a resolution yet (`resolution: null`),
    // but the card should already render the Approved receipt.
    deliverFromEmbedder({
      type: "minds:permission-request-resolved",
      requestId: "fs-1",
      resolution: "granted",
    });
    const card = PermissionCard();
    const vnode = card.view({
      attrs: {
        toolCall: makeToolCall(PERMISSION_INPUT),
        toolResult: makeResult(FILE_SHARING_OUTPUT),
        resolution: null,
      },
    } as unknown as Parameters<typeof card.view>[0]);
    const verdict = findByClass(vnode, "permission-request-verdict");
    expect(verdict).not.toBeNull();
    expect(textOf(verdict)).toBe("Approved");
    expect(findReviewButton(vnode)).toBeNull();
  });
});
