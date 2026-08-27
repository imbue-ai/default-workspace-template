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
  // The notice registers a document keydown listener for Escape; capture it so a test can fire it.
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
    openAgentAuth: vi.fn(),
    listeners,
    agent,
  };
});

vi.mock("../models/Response", () => ({
  sendMessage: mocks.sendMessage,
  drainToComposer: mocks.drainToComposer,
}));
vi.mock("../models/ComposerAttachments", () => ({
  clearComposerAttachments: vi.fn(),
  getComposerAttachments: () => [],
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
vi.mock("../models/request-error", () => ({ describeRequestError: (e: unknown) => String(e) }));
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
vi.mock("../models/AgentAuth", () => ({ openAgentAuth: mocks.openAgentAuth }));
vi.mock("./icons", () => ({ icon: () => "", stopIcon: () => "" }));

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
    mocks.openAgentAuth.mockClear();
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
    // Run the overlay's oncreate so the keydown listener registers, as mithril would on mount.
    const overlay = findByClass(after, "modal-overlay");
    (overlay?.attrs?.oncreate as (() => void) | undefined)?.();

    const keydownHandlers = mocks.listeners.get("keydown") ?? [];
    expect(keydownHandlers.length, "notice should register a keydown listener").toBeGreaterThan(0);
    keydownHandlers.forEach((handler) => handler({ key: "Escape" }));

    const reRendered = component.view!({ attrs: { agentId: "agent-1" } } as never);
    expect(renderedText(reRendered)).not.toContain("can't be sent from chat");
  });

  it("removes the keydown listener when the notice goes away", async () => {
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "/status");
    const overlay = findByClass(after, "modal-overlay");
    (overlay?.attrs?.oncreate as (() => void) | undefined)?.();
    const registered = (mocks.listeners.get("keydown") ?? []).length;
    (overlay?.attrs?.onremove as (() => void) | undefined)?.();
    expect((mocks.listeners.get("keydown") ?? []).length).toBe(registered - 1);
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

    // "Open agent auth" routes through the per-harness dispatch.
    const openButton = findByClass(after, "btn--primary");
    (openButton?.attrs?.onclick as (() => void) | undefined)?.();
    expect(mocks.openAgentAuth).toHaveBeenCalledWith("agent-1");
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
    const stopButton = findByAttr(render(), "aria-label", "Interrupt and bring queued messages to the composer");
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
