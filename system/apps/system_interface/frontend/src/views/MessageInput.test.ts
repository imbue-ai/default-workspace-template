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
  return { sendMessage: vi.fn(async () => {}), listeners };
});

vi.mock("../models/Response", () => ({
  sendMessage: mocks.sendMessage,
  interruptAgent: vi.fn(async () => {}),
  getEventsForAgent: () => [],
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
vi.mock("../models/PendingMessages", () => ({
  addPendingMessage: vi.fn(() => 1),
  getEffectiveActivityState: () => "idle",
  markPendingMessageQueued: vi.fn(),
  removePendingMessage: vi.fn(),
}));
vi.mock("../models/request-error", () => ({ describeRequestError: (e: unknown) => String(e) }));
vi.mock("../models/ModelSettings", () => ({
  fetchModelSettings: vi.fn(),
  getModelSettings: () => null,
  getSelectedOption: () => null,
  setFastMode: vi.fn(),
  setModel: vi.fn(),
}));
vi.mock("../models/ClaudeAuth", () => ({ openLoginModal: vi.fn() }));
vi.mock("./ActivityIndicator", () => ({ isWorkingActivityState: () => false }));
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

/** Render the composer for one agent, type `text`, then press the send button. */
async function typeAndSend(component: m.Component<{ agentId: string | null }>, agentId: string, text: string) {
  const render = () => component.view!({ attrs: { agentId } } as never);
  const textarea = findByTag(render(), "textarea");
  const oninput = textarea?.attrs?.oninput as ((event: unknown) => void) | undefined;
  oninput?.({ target: { value: text, style: {}, scrollHeight: 10 } });

  const sendButton = findByClass(render(), "message-input-send-button");
  const onclick = sendButton?.attrs?.onclick as (() => Promise<void>) | undefined;
  expect(onclick, "send button should be present once text is typed").toBeTruthy();
  await onclick!();
  return render();
}

describe("MessageInput send guard", () => {
  beforeEach(() => {
    mocks.sendMessage.mockClear();
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
    const overlay = findByClass(after, "custom-url-dialog-overlay");
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
    const overlay = findByClass(after, "custom-url-dialog-overlay");
    (overlay?.attrs?.oncreate as (() => void) | undefined)?.();
    const registered = (mocks.listeners.get("keydown") ?? []).length;
    (overlay?.attrs?.onremove as (() => void) | undefined)?.();
    expect((mocks.listeners.get("keydown") ?? []).length).toBe(registered - 1);
  });

  it("does not carry the notice over to another agent", async () => {
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "/status");
    expect(renderedText(after)).toContain("can't be sent from chat");

    const switched = component.view!({ attrs: { agentId: "agent-2" } } as never);
    expect(renderedText(switched)).not.toContain("can't be sent from chat");
  });
});
