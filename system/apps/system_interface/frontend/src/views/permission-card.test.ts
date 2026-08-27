import m from "mithril";
import { describe, expect, it, vi } from "vitest";
import * as embedContract from "@minds/embed-contract";
import { resetEmbedEndpointForTesting } from "../embed";
import type { ToolCall, ToolResultEvent } from "../models/Response";
import type { ScopeInfo } from "./latchkey-scope-info";
import type { PermissionResolution } from "./message-classification";
import {
  PermissionCard,
  isFiledPermissionRequest,
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

function makeToolCall(inputPreview: string, display?: "permission_request"): ToolCall {
  return {
    tool_call_id: "call-1",
    tool_name: "Bash",
    input_preview: inputPreview,
    ...(display ? { display } : {}),
  };
}

function makeResult(output: string, isError = false, permissionRequest?: Record<string, unknown>): ToolResultEvent {
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
    ...(permissionRequest === undefined ? {} : { permission_request: permissionRequest }),
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

// The backend caps tool output (`_MAX_OUTPUT_LENGTH` in session_parser.py) and
// appends this marker. Mirrored here because the recovery scan's real trigger
// is a response past the backend's preservation ceiling
// (`_MAX_PERMISSION_REQUEST_LENGTH`, 8000): such an event arrives with no
// structured `permission_request` field and only this head-truncated body, so
// these fixtures are exactly what the card sees then.
const BACKEND_MAX_OUTPUT_LENGTH = 2000;

function truncateAt(output: string, length: number): string {
  return output.length <= length ? output : `${output.slice(0, length)}...`;
}

function truncateLikeBackend(output: string): string {
  return truncateAt(output, BACKEND_MAX_OUTPUT_LENGTH);
}

// curl's progress meter, which precedes the body and eats into the budget.
const CURL_METER =
  "  % Total    % Received % Xferd  Average Speed   Time    Time     Time  Current\n" +
  "                                 Dload  Upload   Total   Spent    Left  Speed\n" +
  "100  2637  100  2399  100   317  12000   1687 --:--:-- --:--:-- --:--:-- 13000\n";

const TRUNCATED_REQUEST_ID = "c1f0a4b78e9d4f2ab6c35d81e07f42a9";
const TRUNCATED_RATIONALE = "Export a backup of the old workspace so I can diff its notes against this one.";
const TRUNCATED_PERMISSIONS = ["minds-workspaces-backups-export", "minds-workspaces-read", "minds-workspaces-message"];

/** One generated JSON-Schema fragment, the shape the gateway's `computeEffect`
 *  emits per granted verb. Several of these are what push a workspace request's
 *  response past the output cap. */
function verbSchema(verb: string): Record<string, unknown> {
  return {
    name: verb,
    schema: {
      type: "object",
      properties: {
        target_workspace_id: { type: "string", description: "The workspace this verb acts on." },
        confirm: { type: "boolean", description: "Whether the user confirmed this action." },
      },
      required: ["target_workspace_id"],
      additionalProperties: false,
    },
  };
}

// The gateway's create response, in its real shape: pretty-printed at indent 2
// (sendJson), request_id first, and the request's `target` and generated
// `effect` last -- the two fields the card never reads, and the bulk of what
// pushes the response past the cap. Spreading `overrides` last keeps each key in
// the gateway's own order (see handleCreateRequest).
function createResponseOutput(overrides: Record<string, unknown> = {}): string {
  const body = {
    request_id: TRUNCATED_REQUEST_ID,
    agent_id: "agent-a3b7b469ee8341779c9ede1a798c447f",
    rationale: TRUNCATED_RATIONALE,
    request_type: "workspace",
    payload: {
      permissions: TRUNCATED_PERMISSIONS,
      target_workspace_id: "agent-a3b7b469ee8341779c9ede1a798c447f",
    },
    target: "/home/user/.latchkey/permissions.json",
    effect: {
      rules: [{ "minds-workspaces::agent-a3b7b469ee8341779c9ede1a798c447f": ["minds-workspaces-backups-export"] }],
      schemas: TRUNCATED_PERMISSIONS.map(verbSchema),
    },
    ...overrides,
  };
  return `${CURL_METER}${JSON.stringify(body, null, 2)}\n`;
}

/** The index just inside `key`'s value in a create response, for a test that
 *  needs the cut to land in a specific field. */
function insideField(output: string, key: string): number {
  return output.indexOf(`"${key}"`) + key.length + 8;
}

describe("parsePermissionRequest", () => {
  it("parses the rich details of a successful predefined creation POST", () => {
    const result = parsePermissionRequest(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(PERMISSION_OUTPUT),
    );
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
    const result = parsePermissionRequest(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(FILE_SHARING_OUTPUT),
    );
    expect(result).toMatchObject({
      requestId: "fs-1",
      requestType: "file-sharing",
      path: "/Users/you/Documents/report",
      access: "WRITE",
      scope: null,
    });
  });

  it("parses a workspace request's id and type", () => {
    const result = parsePermissionRequest(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(WORKSPACE_OUTPUT),
    );
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
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT, "permission_request"), null)).toBeNull();
  });

  it("returns null when the creation call errored", () => {
    const errored = makeResult("request not permitted by the user", true);
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT, "permission_request"), errored)).toBeNull();
  });

  it("returns null when the output has no JSON body", () => {
    expect(
      parsePermissionRequest(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult("nope")),
    ).toBeNull();
  });

  it("returns null when the JSON body has no request_id", () => {
    expect(
      parsePermissionRequest(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult('{"agent_id":"a"}')),
    ).toBeNull();
  });

  it("prefers the request the backend parsed before it truncated the output", () => {
    // The backend now lifts the response off the untruncated output onto the
    // event. Pair a deliberately unreadable output with that field to prove the
    // field is what's read -- there is nothing in this output to recover from.
    const result = parsePermissionRequest(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult("  % Total    % Received\n{ truncated beyond repai...", false, {
        request_id: "885711ec07bf47239d71294e1534330b",
        rationale: "read the deploy thread",
        request_type: "predefined",
        payload: { scope: "slack-api", permissions: ["slack-read-all"] },
      }),
    );
    expect(result).toEqual({
      requestId: "885711ec07bf47239d71294e1534330b",
      requestType: "predefined",
      rationale: "read the deploy thread",
      scope: "slack-api",
      permissions: ["slack-read-all"],
      path: null,
      access: null,
    });
  });

  it("the truncation fixture really does exceed the backend's output cap", () => {
    // Guards the fixture: if it ever shrinks under the cap, the recovery tests
    // below would silently start exercising the strict parse instead.
    expect(createResponseOutput().length).toBeGreaterThan(BACKEND_MAX_OUTPUT_LENGTH);
  });

  it("recovers a response past the backend's preservation ceiling too", () => {
    // The scan's real reason to exist: past _MAX_PERMISSION_REQUEST_LENGTH
    // (8000) the backend deliberately refuses to preserve the object, so the
    // event arrives with no structured field at all -- an agent's rationale has
    // no length limit, and this is what such a request looks like. The id sits
    // inside the head-truncated body, so the card can still name and open it.
    const oversized = createResponseOutput({ rationale: "x".repeat(9000) });
    expect(oversized.length).toBeGreaterThan(8000);

    const details = parsePermissionRequest(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(truncateLikeBackend(CURL_METER + oversized), false),
    );
    expect(details?.requestId).toBe(TRUNCATED_REQUEST_ID);
  });

  it("recovers the id, rationale, type and payload from a backend-truncated response", () => {
    const result = parsePermissionRequest(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(truncateLikeBackend(createResponseOutput())),
    );
    expect(result).toEqual({
      requestId: TRUNCATED_REQUEST_ID,
      requestType: "workspace",
      rationale: TRUNCATED_RATIONALE,
      scope: null,
      permissions: TRUNCATED_PERMISSIONS,
      path: null,
      access: null,
    });
  });

  it("drops fields that did not survive the cut instead of guessing at them", () => {
    // A cut inside `payload` -- shorter than the real cap, to put the boundary
    // in a chosen field. Recovery is subtractive: the members before it are
    // returned verbatim and the half-written one is dropped, not reconstructed.
    const output = createResponseOutput();
    const result = parsePermissionRequest(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(truncateAt(output, insideField(output, "payload"))),
    );
    expect(result).toMatchObject({
      requestId: TRUNCATED_REQUEST_ID,
      rationale: TRUNCATED_RATIONALE,
      requestType: "workspace",
      scope: null,
      permissions: [],
    });
  });

  it("is not fooled by braces, quotes, or commas inside the rationale", () => {
    const rationale = 'Read {"a": 1}, then "summarise", ok?';
    const output = createResponseOutput({
      rationale,
      request_type: "predefined",
      payload: { scope: "slack-api", permissions: ["slack-read-all"] },
    });
    const result = parsePermissionRequest(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(truncateLikeBackend(output)),
    );
    expect(result?.rationale).toBe(rationale);
    expect(result?.scope).toBe("slack-api");
  });

  it("returns null when the cut falls before the first complete field", () => {
    const output = createResponseOutput();
    const cut = truncateAt(output, output.indexOf("{") + 40);
    expect(parsePermissionRequest(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(cut))).toBeNull();
  });

  it("returns null when a truncated body's request id is not gateway-generated", () => {
    // The recovery path reconstructs the id's text, and that id is handed
    // straight to the shell's modal, so it must look gateway-minted.
    const output = createResponseOutput({ request_id: "not-a-uuid" });
    expect(
      parsePermissionRequest(
        makeToolCall(PERMISSION_INPUT, "permission_request"),
        makeResult(truncateLikeBackend(output)),
      ),
    ).toBeNull();
  });

  it("returns null for a truncated gateway error body", () => {
    const errorBody = '{\n  "error": "Invalid request body.",\n  "status": 400,\n  "detail": "rat';
    expect(
      parsePermissionRequest(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(errorBody)),
    ).toBeNull();
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
    // A closure-component vnode (e.g. m(Button, ...)) carries no markup of its
    // own -- its view runs only when mithril renders it -- so expand it and
    // search what it renders.
    if (typeof vnode.tag === "function") {
      const component = (vnode.tag as (v: unknown) => { view: (v: unknown) => unknown })(vnode);
      return findVnode(component.view(vnode), pred);
    }
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
// which is also a <button>). It moved onto the shared .btn primitive; btn--primary
// is unique to it in this render.
function findReviewButton(node: unknown): { attrs?: Record<string, unknown>; children?: unknown } | null {
  return findByClass(node, "btn--primary");
}

// Every glyph on the card is an `m.trust`ed SVG string, which `findVnode` walks
// straight past, so glyphs are asserted as markup rather than as vnode attrs.
function trustedHtmlIn(node: unknown): string {
  if (Array.isArray(node)) return node.map(trustedHtmlIn).join("");
  if (node !== null && typeof node === "object") {
    const vnode = node as { tag?: unknown; children?: unknown };
    if (vnode.tag === "<" && typeof vnode.children === "string") return vnode.children;
    return trustedHtmlIn(vnode.children);
  }
  return "";
}

// The `src` of the first <img> under a node, or null when there is none.
function markSrc(node: unknown): string | null {
  const image = findVnode(node, (v) => v.tag === "img") as { attrs?: { src?: unknown } } | null;
  return typeof image?.attrs?.src === "string" ? image.attrs.src : null;
}

// Pinned as literals so a card that goes back to a padlock fails here rather
// than at review. (`lock` is gone from the icon set, so this is a regression
// guard for re-adding it, not for a name that still resolves.)
const KEY_PATH = 'd="M2.586 17.414';
const CUBE_PATH = 'd="M21 8a2 2 0 0 0-1-1.73';
const LOCK_RECT = '<rect x="3" y="11"';

// A predefined request naming a service the workspace bundles no artwork for.
const UNBUNDLED_SERVICE_OUTPUT =
  '{"request_id":"x1","request_type":"predefined","rationale":"look something up","payload":{"scope":"madeup-api"}}';

describe("isFiledPermissionRequest", () => {
  // A PreToolUse guard refusing the command is the common way this happens: the
  // harness returns the block message as an errored result and nothing ever
  // reaches the gateway. Rendering that as a permission card sent the user to a
  // Permissions tab with nothing in it.
  const call = makeToolCall(PERMISSION_INPUT, "permission_request");

  it("counts a request whose result has not arrived yet", () => {
    expect(isFiledPermissionRequest(call, null)).toBe(true);
  });

  it("counts a request the gateway answered", () => {
    expect(isFiledPermissionRequest(call, makeResult(PERMISSION_OUTPUT))).toBe(true);
  });

  it("does not count a call the harness refused", () => {
    const blocked = makeResult("PreToolUse:Bash hook error: Blocked: file ONE latchkey permission request", true);
    expect(isFiledPermissionRequest(call, blocked)).toBe(false);
  });

  it("does not count a body the gateway rejected", () => {
    // curl exits 0 on a 4xx, so this does not even read as a failed call -- but no
    // request was created, so there is nothing in the Permissions tab to send the
    // user to. Verbatim from a gateway that refused a malformed payload.
    const rejected = makeResult(
      '{\n  "error": "Invalid request body: payload.\'scope\' is required and must be a non-empty string."\n}',
    );
    expect(isFiledPermissionRequest(call, rejected)).toBe(false);
  });

  it("does not count an ordinary tool call", () => {
    expect(isFiledPermissionRequest(makeToolCall('{"command":"echo hi"}'), null)).toBe(false);
  });
});

describe("renderPermissionCard", () => {
  it("shows the eyebrow, title, rationale, and review button on a pending card", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(PERMISSION_OUTPUT));

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
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(PERMISSION_OUTPUT));
    const button = findReviewButton(vnode) as { attrs?: { onclick?: (e: Event) => void } } | null;

    resetEmbedEndpointForTesting();
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
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), null);

    expect(textOf(findByClass(vnode, "permission-request-eyebrow"))).toBe("Permission request");
    expect(findReviewButton(vnode)).toBeNull();
    expect(textOf(findByClass(vnode, "permission-request-status"))).toBe("Waiting for the request to register\u2026");
  });

  it("shows a short unreadable status, not a paragraph, when nothing could be recovered", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult('{"agent_id":"a"}'));

    expect(findReviewButton(vnode)).toBeNull();
    expect(textOf(findByClass(vnode, "permission-request-status"))).toBe(
      "Couldn't read this request \u2014 see the Permissions tab.",
    );
    // The toggle is this state's only control, so it must not disappear with
    // the rest of the card.
    expect(findByClass(vnode, "permission-request-raw-toggle")).not.toBeNull();
  });

  it("renders a titled card with a working review button for a backend-truncated pending request", () => {
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(truncateLikeBackend(createResponseOutput())),
    );

    expect(textOf(findByClass(vnode, "permission-request-title"))).toBe("Other machines");
    expect(textOf(findByClass(vnode, "permission-request-reason"))).toBe(TRUNCATED_RATIONALE);
    // The apologetic fallback is not what a still-pending request shows.
    expect(findByClass(vnode, "permission-request-status")).toBeNull();

    const button = findReviewButton(vnode) as { attrs?: { onclick?: (e: Event) => void } } | null;
    expect(textOf(button)).toBe("Review & respond");
    const postMessage = vi.fn();
    withStubbedEmbedder({ postMessage }, () =>
      button?.attrs?.onclick?.({ preventDefault() {}, stopPropagation() {} } as unknown as Event),
    );
    expect(postMessage).toHaveBeenCalledWith(
      { type: "minds:open-request-modal", requestId: TRUNCATED_REQUEST_ID },
      "*",
    );
  });

  it("shows the rationale and button, and no redundant title, when the type was lost to the cut", () => {
    // Cut inside `request_type`: the rationale survives but nothing names the
    // subject, so a generic title row would only repeat the eyebrow.
    const output = createResponseOutput();
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(truncateAt(output, insideField(output, "request_type"))),
    );

    expect(findByClass(vnode, "permission-request-title")).toBeNull();
    expect(textOf(findByClass(vnode, "permission-request-reason"))).toBe(TRUNCATED_RATIONALE);
    expect(findByClass(vnode, "permission-request-status")).toBeNull();
    expect(findReviewButton(vnode)).not.toBeNull();
  });

  it("falls back to the generic subject when neither a title nor a rationale survived", () => {
    // Cut inside `rationale`: only the id came back, and the body must not be a
    // bare badge, so the generic subject stands in.
    const output = createResponseOutput();
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(truncateAt(output, insideField(output, "rationale"))),
    );

    expect(textOf(findByClass(vnode, "permission-request-title"))).toBe("Permission request");
    expect(findByClass(vnode, "permission-request-reason")).toBeNull();
    expect(findReviewButton(vnode)).not.toBeNull();
  });

  it("keeps the raw disclosure on a truncated card so the dropped fields stay reachable", () => {
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(truncateLikeBackend(createResponseOutput())),
      null,
      null,
      true,
    );

    expect(textOf(findByClass(vnode, "permission-request-raw-toggle"))).toBe("Hide raw request");
    const raw = findByClass(vnode, "permission-request-raw");
    const rawTextNode = findVnode(
      raw,
      (v) => v.tag === "#" && typeof (v as { children?: unknown }).children === "string",
    );
    expect(rawTextNode?.children as string).toContain("...");
  });

  it("titles a file-sharing request 'Local files'", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(FILE_SHARING_OUTPUT));
    expect(textOf(findByClass(vnode, "permission-request-title"))).toBe("Local files");
  });

  it("titles an accounts request 'Device accounts'", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(ACCOUNTS_OUTPUT));
    expect(textOf(findByClass(vnode, "permission-request-title"))).toBe("Device accounts");
  });

  it("titles a workspace request 'Other machines' with a button and no permission specifics", () => {
    const vnode = renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(WORKSPACE_OUTPUT));

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
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(PERMISSION_OUTPUT),
      "granted",
    );
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "Approved"),
    ).not.toBeNull();
    // The compact receipt still names what was requested.
    expect(textOf(findByClass(vnode, "permission-request-receipt-title"))).toBe("slack-api");
    // The action button is replaced by the verdict receipt.
    expect(findReviewButton(vnode)).toBeNull();
  });

  it("shows a Denied receipt once denied", () => {
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(PERMISSION_OUTPUT),
      "denied",
    );
    expect(
      findVnode(vnode, (v) => v.tag === "#" && (v as { children?: unknown }).children === "Denied"),
    ).not.toBeNull();
    expect(findReviewButton(vnode)).toBeNull();
  });

  it("shows a couldn't-complete receipt for an error outcome", () => {
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(PERMISSION_OUTPUT),
      "error",
    );
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
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(PERMISSION_OUTPUT),
      null,
      scopeInfo,
    );
    expect(textOf(findByClass(vnode, "permission-request-title"))).toBe("Slack");
  });

  it("offers a closed raw disclosure whose toggle reports the click", () => {
    const onToggleRaw = vi.fn();
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
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
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(PERMISSION_OUTPUT),
      null,
      null,
      true,
    );

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
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(PERMISSION_OUTPUT),
      "granted",
      null,
      true,
    );
    expect(textOf(findByClass(vnode, "permission-request-raw-toggle"))).toBe("Hide raw request");
    expect(findByClass(vnode, "permission-request-raw")).not.toBeNull();
  });

  it("heads every card state with a key, and never brings the lock back", () => {
    const states: [string, m.Vnode][] = [
      ["pending", renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(PERMISSION_OUTPUT))],
      [
        "resolved",
        renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(PERMISSION_OUTPUT), "granted"),
      ],
      [
        "unreadable",
        renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult('{"agent_id":"a"}')),
      ],
    ];
    for (const [state, vnode] of states) {
      expect(trustedHtmlIn(findByClass(vnode, "permission-request-eyebrow")), state).toContain(KEY_PATH);
      expect(trustedHtmlIn(vnode), state).not.toContain(LOCK_RECT);
    }
  });

  it("badges a service request with that service's own mark", () => {
    const badge = findByClass(
      renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(PERMISSION_OUTPUT)),
      "permission-request-badge",
    );
    expect(markSrc(badge)).toContain("slack");
    // The mark replaces the glyph rather than sitting beside it.
    expect(trustedHtmlIn(badge)).toBe("");
  });

  it("badges a request that names no app with the cube", () => {
    // A file-sharing request is about local files; there is no app to show.
    const badge = findByClass(
      renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(FILE_SHARING_OUTPUT)),
      "permission-request-badge",
    );
    expect(trustedHtmlIn(badge)).toContain(CUBE_PATH);
    expect(markSrc(badge)).toBeNull();
  });

  it("badges a service we ship no mark for with the cube", () => {
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult(UNBUNDLED_SERVICE_OUTPUT),
    );
    const badge = findByClass(vnode, "permission-request-badge");
    expect(trustedHtmlIn(badge)).toContain(CUBE_PATH);
    expect(markSrc(badge)).toBeNull();
    // The card still names the service, so the cube reads as the artwork's
    // absence and not as a card that failed to read the request.
    expect(textOf(findByClass(vnode, "permission-request-title"))).toBe("madeup-api");
  });

  it("keeps the service mark on the resolved receipt", () => {
    const badge = findByClass(
      renderCardFor(makeToolCall(PERMISSION_INPUT, "permission_request"), makeResult(PERMISSION_OUTPUT), "granted"),
      "permission-request-badge--sm",
    );
    expect(markSrc(badge)).toContain("slack");
  });

  it("badges a receipt whose request could not be parsed with the cube", () => {
    // The receipt renders before the unreadable-request branch, so `details` is
    // null there and the badge must still have something to draw.
    const vnode = renderCardFor(
      makeToolCall(PERMISSION_INPUT, "permission_request"),
      makeResult('{"agent_id":"a"}'),
      "granted",
    );
    const badge = findByClass(vnode, "permission-request-badge--sm");
    expect(trustedHtmlIn(badge)).toContain(CUBE_PATH);
    expect(textOf(findByClass(vnode, "permission-request-receipt-title"))).toBe("Permission request");
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

// The instant card flip works only once the vendored embed contract defines
// PERMISSION_REQUEST_RESOLVED: the workspace endpoint validates incoming types
// against the vendored contract and drops unknown ones before any handler
// runs (mngr-internal#224 adds the type; this repo deliberately does not edit
// system/vendor by hand). Until the release sync lands, cards fall back to
// the transcript-driven flip and these tests skip -- and un-skip themselves
// the moment the sync arrives, since the probe reads the vendored contract
// itself.
const HAS_RESOLVED_MESSAGE = "PERMISSION_REQUEST_RESOLVED" in embedContract;

describe.skipIf(!HAS_RESOLVED_MESSAGE)("shell permission resolutions", () => {
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
        toolCall: makeToolCall(PERMISSION_INPUT, "permission_request"),
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
