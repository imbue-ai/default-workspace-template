import { describe, expect, it, vi, beforeEach } from "vitest";
import type m from "mithril";

// vi.mock factories are hoisted above module scope, so anything they close over must come from
// vi.hoisted. Mithril also captures requestAnimationFrame at import time, and the composer reads
// localStorage, so both are polyfilled here too.
const mocks = vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
  const store = new Map<string, string>();
  globalThis.localStorage ??= {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: () => null,
    length: 0,
  } as Storage;
  // The node test env has no document; provide a minimal one for code that wires
  // document listeners when lifecycle hooks actually run (they don't in these
  // vnode-only tests, but imports must not explode).
  const listeners = new Map<string, ((event: unknown) => void)[]>();
  globalThis.document ??= {
    addEventListener: (type: string, fn: (event: unknown) => void) => {
      listeners.set(type, [...(listeners.get(type) ?? []), fn]);
    },
    removeEventListener: (type: string, fn: (event: unknown) => void) => {
      listeners.set(
        type,
        (listeners.get(type) ?? []).filter((f) => f !== fn),
      );
    },
  } as unknown as Document;
  // The composer gates its Claude-only interceptions on the agent's harness and its
  // working/idle branch (stop button, placeholder) on the agent's activity state --
  // both driven through this mutable holder (reset in beforeEach; an undefined
  // activity_state means "not working").
  const agent: { harness: string | undefined; activity_state: string | undefined } = {
    harness: "claude",
    activity_state: undefined,
  };
  return {
    sendMessage: vi.fn(async () => {}),
    drainToComposer: vi.fn(async () => ({ block: "" })),
    getComposerAttachments: vi.fn(() => [] as unknown[]),
    interruptAgent: vi.fn(async () => {}),
    openProviderChooser: vi.fn(),
    listeners,
    agent,
  };
});

vi.mock("../models/Response", () => ({
  sendMessage: mocks.sendMessage,
  drainToComposer: mocks.drainToComposer,
  interruptAgent: mocks.interruptAgent,
}));
vi.mock("../models/ComposerAttachments", () => ({
  clearComposerAttachments: vi.fn(),
  getComposerAttachments: mocks.getComposerAttachments,
  getReadyAttachmentPaths: () => [],
  hasReadyAttachments: () => false,
  removeComposerAttachment: vi.fn(),
  restoreComposerAttachments: vi.fn(),
  uploadFilesToComposer: vi.fn(),
  waitForComposerUploads: vi.fn(async () => {}),
}));
vi.mock("../models/attachments", () => ({
  buildMessageWithAttachments: (text: string) => text,
  formatFileSize: () => "0 B",
}));
vi.mock("../../models/request-error", () => ({
  describeRequestError: (e: unknown) => String(e),
  describeRequestErrorKind: (e: unknown) =>
    typeof e === "object" && e !== null && "kind" in e ? (e as { kind: string }).kind : "unknown",
}));
vi.mock("../models/ModelSettings", () => ({
  effectiveChoice: () => null,
  isPickInFlight: () => false,
  setModelChoice: vi.fn(),
}));
// The composer guard follows whatever popups the harness declared on its catalog,
// so the mock ships a per-harness fixture mirroring the real declarations (the
// matcher itself is reimplemented here minimally; the real one is covered by
// HarnessCatalog.test.ts).
vi.mock("../models/HarnessCatalog", () => {
  const catalogs: Record<string, { popups: { trigger: string; commands: string[]; action: string }[] }> = {
    claude: {
      popups: [
        { trigger: "composer_command", commands: ["/login", "/logout"], action: "open_auth" },
        { trigger: "composer_command", commands: ["/status", "/exit"], action: "notice" },
      ],
    },
    codex: {
      popups: [
        { trigger: "composer_command", commands: ["/login", "/logout"], action: "open_auth" },
        { trigger: "composer_command", commands: ["/new", "/fast"], action: "notice" },
      ],
    },
  };
  const getHarnessCatalog = (harness?: string) => (harness ? (catalogs[harness] ?? null) : null);
  return {
    ensureHarnessCatalogs: vi.fn(async () => {}),
    getHarnessCatalog,
    findComposerPopup: (harness: string | undefined, text: string) => {
      const firstToken = text.trim().toLowerCase().split(/\s+/, 1)[0] ?? "";
      for (const popup of getHarnessCatalog(harness)?.popups ?? []) {
        if (popup.trigger === "composer_command" && popup.commands.includes(firstToken)) {
          return { popup, command: firstToken };
        }
      }
      return null;
    },
  };
});
vi.mock("../models/AgentManager", () => ({ getAgentById: () => mocks.agent }));
vi.mock("../../models/Providers", () => ({ openProviderChooser: mocks.openProviderChooser }));
vi.mock("../../views/icons", () => ({ icon: () => "", stopIcon: () => "" }));

import { MessageInput } from "./MessageInput";

type AnyVnode = { tag?: unknown; attrs?: Record<string, unknown>; children?: unknown; text?: unknown };

/** Every vnode in the tree, depth-first. */
function flatten(node: unknown): AnyVnode[] {
  if (node === null || node === undefined || typeof node !== "object") {
    return [];
  }
  if (Array.isArray(node)) {
    return node.flatMap(flatten);
  }
  const vnode = node as AnyVnode;
  // A closure-component vnode (e.g. m(Button, ...)) carries no markup of its
  // own -- its view runs only when mithril renders it -- so expand it and
  // flatten what it renders.
  if (typeof vnode.tag === "function") {
    const component = (vnode.tag as (v: AnyVnode) => { view: (v: AnyVnode) => unknown })(vnode);
    return flatten(component.view(vnode));
  }
  // Likewise for object component vnodes (an instance with a view method),
  // e.g. NoticeDialog: their markup does not exist in this tree until the
  // view is asked for it.
  const tag = vnode.tag as unknown;
  if (tag !== null && typeof tag === "object" && typeof (tag as { view?: unknown }).view === "function") {
    // Children come along too: NoticeDialog renders its body through the Modal
    // shell, which reads it off vnode.children.
    const rendered = (tag as { view: (v: unknown) => unknown }).view({
      attrs: vnode.attrs ?? {},
      children: vnode.children,
    });
    return [vnode, ...flatten(rendered)];
  }
  return [vnode, ...flatten(vnode.children)];
}

/** All literal text rendered in the tree, so a notice can be asserted on by its wording. */
function renderedText(node: unknown): string {
  return flatten(node)
    .map((vnode) =>
      typeof vnode.text === "string" ? vnode.text : typeof vnode.children === "string" ? vnode.children : "",
    )
    .join(" ");
}

function findByClass(node: unknown, className: string): AnyVnode | undefined {
  // Mithril's hyperscript normalizes a `class` attribute onto `className`, so check both.
  return flatten(node).find((vnode) => {
    const attrs = vnode.attrs ?? {};
    return [attrs.class, attrs.className].some((v) => typeof v === "string" && v.includes(className));
  });
}

/** The first onEscape guard in the tree -- the attr a dialog hands the Modal shell. Walks the
 *  raw vnodes rather than flatten(), which drops closure-component vnodes (Modal is one). */
function findEscapeGuard(node: unknown): (() => void) | undefined {
  if (node === null || node === undefined || typeof node !== "object") {
    return undefined;
  }
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findEscapeGuard(child);
      if (found !== undefined) return found;
    }
    return undefined;
  }
  const vnode = node as AnyVnode;
  if (typeof vnode.attrs?.onEscape === "function") {
    return vnode.attrs.onEscape as () => void;
  }
  if (typeof vnode.tag === "function") {
    const component = (vnode.tag as (v: AnyVnode) => { view: (v: AnyVnode) => unknown })(vnode);
    return findEscapeGuard(component.view(vnode));
  }
  const tag = vnode.tag as unknown;
  if (tag !== null && typeof tag === "object" && typeof (tag as { view?: unknown }).view === "function") {
    const rendered = (tag as { view: (v: unknown) => unknown }).view({
      attrs: vnode.attrs ?? {},
      children: vnode.children,
    });
    return findEscapeGuard(rendered);
  }
  return findEscapeGuard(vnode.children);
}

/** Let queued promise callbacks run. The notice's buttons are `() => void action()`, which is
 *  right for mithril but discards the promise, so awaiting the handler does not await the work. */
async function flushAsync(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

/** Find a button by its visible label. The tooltip is not inspectable -- hoverTooltipAttrs
 *  returns lifecycle hooks rather than an attribute -- so the label is the stable handle. */
function findButton(node: unknown, label: string): AnyVnode | undefined {
  return flatten(node).find((vnode) => vnode.tag === "button" && renderedText(vnode).includes(label));
}

function findByTag(node: unknown, tag: string): AnyVnode | undefined {
  return flatten(node).find((vnode) => vnode.tag === tag);
}

/** Find a vnode by an exact attribute value (e.g. a stable aria-label). */
function findByAttr(node: unknown, attr: string, value: string): AnyVnode | undefined {
  return flatten(node).find((vnode) => (vnode.attrs ?? {})[attr] === value);
}

/** Render the composer for one agent, type `text`, then press the send button. */
async function typeAndSend(component: m.Component<{ agentId: string | null }>, agentId: string, text: string) {
  const render = () => component.view!({ attrs: { agentId } } as never);
  const textarea = findByTag(render(), "textarea");
  const oninput = textarea?.attrs?.oninput as ((event: unknown) => void) | undefined;
  oninput?.({ target: { value: text, style: {}, scrollHeight: 10 } });

  const sendButton = findByAttr(render(), "aria-label", "Send message");
  const onclick = sendButton?.attrs?.onclick as (() => Promise<void>) | undefined;
  expect(onclick, "send button should be present once text is typed").toBeTruthy();
  await onclick!();
  return render();
}

describe("MessageInput send guard", () => {
  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.openProviderChooser.mockClear();
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    localStorage.clear();
  });

  it("does not send /status, and explains why", async () => {
    const after = await typeAndSend(MessageInput(), "agent-1", "/status");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    const text = renderedText(after);
    expect(text).toContain("/status can't be sent from chat");
    expect(text).toContain("You can still send it from the agent's terminal.");
  });

  it("keeps the typed message so it is not lost", async () => {
    const component = MessageInput();
    await typeAndSend(component, "agent-1", "/status");
    const textarea = findByTag(component.view!({ attrs: { agentId: "agent-1" } } as never), "textarea");
    expect(textarea?.attrs?.value).toBe("/status");
  });

  it("still sends an ordinary message", async () => {
    await typeAndSend(MessageInput(), "agent-1", "hello there");
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "hello there");
  });

  it("still sends a slash command that does not take over the input box", async () => {
    await typeAndSend(MessageInput(), "agent-1", "/clear");
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "/clear");
  });

  it("does not send /exit either, with the same notice", async () => {
    const after = await typeAndSend(MessageInput(), "agent-1", "/exit");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    const text = renderedText(after);
    expect(text).toContain("/exit can't be sent from chat");
    expect(text).toContain("You can still send it from the agent's terminal.");
  });

  it("dismisses the notice on Escape", async () => {
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "/status");
    // The notice hands its Escape guard to the Modal shell via onEscape; the
    // shell's real document listener is covered in NoticeDialog.test.ts (these
    // tests render vnodes with no DOM), so fire the guard directly here.
    const escapeGuard = findEscapeGuard(after);
    expect(escapeGuard, "notice should hand the shell an Escape guard").toBeTypeOf("function");
    escapeGuard!();

    const reRendered = component.view!({ attrs: { agentId: "agent-1" } } as never);
    expect(renderedText(reRendered)).not.toContain("can't be sent from chat");
  });

  it("sends a command another harness never declared", async () => {
    // /status is claude's declaration; codex declared its own list, and /status
    // is not on it, so for a codex agent it goes through with no notice.
    mocks.agent.harness = "codex";
    const after = await typeAndSend(MessageInput(), "agent-1", "/status");
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "/status");
    expect(renderedText(after)).not.toContain("can't be sent from chat");
  });

  it("declines the commands the other harness declared", async () => {
    mocks.agent.harness = "codex";
    const after = await typeAndSend(MessageInput(), "agent-1", "/new");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(renderedText(after)).toContain("/new can't be sent from chat");
  });

  it("intercepts an auth command with the agent-auth notice, even with arguments", async () => {
    const component = MessageInput();
    // The argument form must intercept too: matched on the first token, so
    // "/login please" cannot slip past the guard mid-fetch or mid-typo.
    const after = await typeAndSend(component, "agent-1", "/login please");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(renderedText(after)).toContain("Sign-in is managed here");

    // The notice offers the provider chooser, which signs in to an account of its
    // own rather than running the agent's auth flow inside the agent's terminal.
    const openButton = findByClass(after, "btn--primary");
    (openButton?.attrs?.onclick as (() => void) | undefined)?.();
    expect(mocks.openProviderChooser).toHaveBeenCalled();
  });

  it("intercepts auth commands for every harness that declared them", async () => {
    mocks.agent.harness = "codex";
    const after = await typeAndSend(MessageInput(), "agent-1", "/login");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(renderedText(after)).toContain("Sign-in is managed here");
  });

  it("does not carry the notice over to another agent", async () => {
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "/status");
    expect(renderedText(after)).toContain("can't be sent from chat");

    const switched = component.view!({ attrs: { agentId: "agent-2" } } as never);
    expect(renderedText(switched)).not.toContain("can't be sent from chat");
  });
});

describe("MessageInput placeholder", () => {
  beforeEach(() => {
    mocks.agent.activity_state = undefined;
    localStorage.clear();
  });

  it("shows the base wording while the agent is idle", () => {
    const textarea = findByTag(MessageInput().view!({ attrs: { agentId: "agent-1" } } as never), "textarea");
    expect(textarea?.attrs?.placeholder).toBe("Type a message...");
  });

  it("teaches queueing while the agent has a turn in flight", () => {
    mocks.agent.activity_state = "THINKING";
    const textarea = findByTag(MessageInput().view!({ attrs: { agentId: "agent-1" } } as never), "textarea");
    expect(textarea?.attrs?.placeholder).toBe("Type to queue more messages...");
  });
});

describe("MessageInput stop-to-composer handback", () => {
  beforeEach(() => {
    mocks.drainToComposer.mockReset();
    mocks.drainToComposer.mockResolvedValue({ block: "" });
    mocks.agent.harness = "claude";
    // Working -> the stop button is rendered.
    mocks.agent.activity_state = "THINKING";
    localStorage.clear();
  });

  function typeDraft(component: m.Component<{ agentId: string | null }>, agentId: string, text: string): void {
    const render = () => component.view!({ attrs: { agentId } } as never);
    const textarea = findByTag(render(), "textarea");
    (textarea?.attrs?.oninput as (event: unknown) => void)?.({ target: { value: text, style: {}, scrollHeight: 10 } });
  }

  async function clickStop(
    component: m.Component<{ agentId: string | null }>,
    agentId: string,
  ): Promise<AnyVnode | undefined> {
    const render = () => component.view!({ attrs: { agentId } } as never);
    // The label states what the press will do; with nothing queued (this mock
    // agent has no queued_messages) it reads as a plain interrupt.
    const stopButton = findByAttr(render(), "aria-label", "Interrupt agent");
    const onclick = stopButton?.attrs?.onclick as (() => Promise<void>) | undefined;
    expect(onclick, "stop button should be present while the agent works").toBeTruthy();
    await onclick!();
    return findByTag(render(), "textarea");
  }

  it("prepends the handed-back block above a non-empty draft", async () => {
    mocks.drainToComposer.mockResolvedValueOnce({ block: "queued one\nqueued two" });
    const component = MessageInput();
    typeDraft(component, "agent-1", "my draft");
    const textarea = await clickStop(component, "agent-1");
    expect(textarea?.attrs?.value).toBe("queued one\nqueued two\n\nmy draft");
  });

  it("drops the block straight in when the composer is empty", async () => {
    mocks.drainToComposer.mockResolvedValueOnce({ block: "queued one" });
    const component = MessageInput();
    const textarea = await clickStop(component, "agent-1");
    expect(textarea?.attrs?.value).toBe("queued one");
  });

  it("leaves a non-empty draft untouched on an empty handback", async () => {
    mocks.drainToComposer.mockResolvedValueOnce({ block: "" });
    const component = MessageInput();
    typeDraft(component, "agent-1", "keep me");
    const textarea = await clickStop(component, "agent-1");
    expect(textarea?.attrs?.value).toBe("keep me");
  });
});

describe("MessageInput send failure notice", () => {
  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.openProviderChooser.mockClear();
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    mocks.getComposerAttachments.mockReturnValue([]);
    localStorage.clear();
  });

  it("shows the reason the send was refused, in the workspace", async () => {
    // Rejecting with a plain string: describeRequestError is mocked as String(e), so an Error
    // would render as "Error: ...". mockRejectedValueOnce, not mockRejectedValue -- the mock is
    // module-wide and only cleared between tests, so a persistent rejection leaks into others.
    mocks.sendMessage.mockRejectedValueOnce("The agent is in shell mode with an unsubmitted command.");
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Couldn't send your message");
    expect(text).toContain("shell mode with an unsubmitted command");
    // A failed send is recoverable, so it offers actions rather than a bare acknowledgement.
    expect(text).toContain("Cancel");
  });

  it("dismisses on OK, leaving the restored draft alone", async () => {
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    const okButton = findByClass(after, "notice-dismiss");
    expect(okButton, "the notice should offer an OK button").toBeTruthy();
    (okButton!.attrs!.onclick as () => void)();
    const dismissed = component.view!({ attrs: { agentId: "agent-1" } } as never);
    expect(renderedText(dismissed)).not.toContain("Couldn't send your message");
  });

  it("does not follow the user to another agent", async () => {
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const component = MessageInput();
    await typeAndSend(component, "agent-1", "hello");
    const otherAgent = component.view!({ attrs: { agentId: "agent-2" } } as never);
    expect(renderedText(otherAgent)).not.toContain("Couldn't send your message");
  });

  it("refuses to send when an attachment failed to upload, and names it", async () => {
    // The failed upload is dropped from the message and its chip cleared, so without this the
    // file would vanish with no explanation.
    // Persistent, not Once: the view reads the attachments before handleSend does.
    mocks.getComposerAttachments.mockReturnValue([
      { localId: "a1", fileName: "notes.pdf", status: "error", error: "boom" },
    ]);
    const after = await typeAndSend(MessageInput(), "agent-1", "here you go");
    mocks.getComposerAttachments.mockReturnValue([]);
    const text = renderedText(after);
    expect(text).toContain("didn't upload");
    expect(text).toContain("notes.pdf");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
  });

  it("withholds Retry when the agent is unreachable, since it cannot help", async () => {
    // A pane that is gone will not be there on the next attempt. Force restarts the agent, which
    // is the only thing that can deliver the message, so it stays.
    mocks.sendMessage.mockRejectedValueOnce({ kind: "agent_unreachable", toString: () => "pane is gone" });
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Force");
    expect(text).not.toContain("Retry");
    expect(text).toContain("restarting it is the only way");
  });

  it("keeps Retry for a blocked input, which a person can clear", async () => {
    mocks.sendMessage.mockRejectedValueOnce({ kind: "input_blocked", toString: () => "a dialog is open" });
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Retry");
    expect(text).toContain("Force");
  });

  it("removes the delivered message even when Force drained a queue block above it", async () => {
    // Force prepends the rescued queue block BEFORE sending, so the delivered message is no
    // longer at the front of the composer -- a prefix-only strip would leave it there, sent and
    // still in the box.
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "my message");
    mocks.drainToComposer.mockResolvedValueOnce({ block: "queued one" });

    const force = findButton(after, "Force");
    (force!.attrs!.onclick as () => void)();
    await flushAsync();

    const composer = localStorage.getItem("message-text:agent-1") ?? "";
    expect(composer).toContain("queued one");
    expect(composer).not.toContain("my message");
  });

  it("shows nothing when the send succeeds", async () => {
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    expect(renderedText(after)).not.toContain("Couldn't send your message");
  });

  it("offers Cancel, Retry and Force for a failed send", async () => {
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Cancel");
    expect(text).toContain("Retry");
    expect(text).toContain("Force");
  });

  it("puts the message back in the composer immediately, not only on Cancel", async () => {
    // The recovery record is closure state, so holding the message only there would lose it on
    // a reload or a closed tab. It is persisted the moment the send fails (contract A1a).
    mocks.sendMessage.mockRejectedValueOnce("nope");
    await typeAndSend(MessageInput(), "agent-1", "hello there");
    expect(localStorage.getItem("message-text:agent-1")).toContain("hello there");
  });

  it("prepends the failed message above a draft typed while the send was in flight", async () => {
    // The newer draft lands in storage while the request is in flight, which is exactly the
    // case the old "restore only into an empty composer" guard existed for.
    mocks.sendMessage.mockImplementationOnce(async () => {
      localStorage.setItem("message-text:agent-1", "newer draft");
      throw "nope";
    });
    await typeAndSend(MessageInput(), "agent-1", "failed message");
    const restored = localStorage.getItem("message-text:agent-1") ?? "";
    expect(restored).toContain("failed message");
    expect(restored).toContain("newer draft");
    expect(restored.indexOf("failed message")).toBeLessThan(restored.indexOf("newer draft"));
  });

  // Escape-dismisses-as-Cancel is not covered here: these tests render vnodes with no DOM, so
  // there is no document to dispatch a keydown at. The handler delegates to the same function
  // the Cancel button calls, which is the whole of the fix.

  it("retries the same message through the ordinary send", async () => {
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    mocks.sendMessage.mockClear();

    const retry = findButton(after, "Retry");
    (retry!.attrs!.onclick as () => void)();
    await flushAsync();
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "hello");
  });

  it("force restarts the agent before sending, in that order", async () => {
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    mocks.sendMessage.mockClear();
    mocks.interruptAgent.mockClear();

    const force = findButton(after, "Force");
    (force!.attrs!.onclick as () => void)();
    await flushAsync();
    expect(mocks.drainToComposer).toHaveBeenCalledWith("agent-1");
    expect(mocks.interruptAgent).toHaveBeenCalledWith("agent-1");
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "hello");
  });

  it("does not send when the restart itself fails", async () => {
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    mocks.sendMessage.mockClear();
    mocks.interruptAgent.mockRejectedValueOnce("restart refused");

    const force = findButton(after, "Force");
    (force!.attrs!.onclick as () => void)();
    await flushAsync();
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    const shown = component.view!({ attrs: { agentId: "agent-1" } } as never);
    expect(renderedText(shown)).toContain("restart refused");
  });
});
