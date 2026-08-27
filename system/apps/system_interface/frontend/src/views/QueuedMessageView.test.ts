import { describe, expect, it, vi, beforeEach } from "vitest";
import type { QueuedMessage } from "../models/AgentManager";

// vi.mock factories are hoisted, so shared state comes from vi.hoisted. Mithril
// itself is NOT mocked (its hyperscript builds the vnodes these tests inspect).
const mocks = vi.hoisted(() => {
  // Mithril's redraw schedules through requestAnimationFrame; polyfill it so the
  // view's m.redraw() calls don't throw in the node test environment.
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
  return {
    queued: [] as QueuedMessage[],
    // The backend-reported shoulder-tap availability -- the ONLY thing that decides the
    // button's enabled state (besides the local double-fire guard). Default true.
    available: true,
    shoulderTap: vi.fn(async () => ({ status: "tapped" })),
  };
});

vi.mock("../models/AgentManager", () => ({
  getQueuedMessagesForAgent: () => mocks.queued,
  getShoulderTapAvailableForAgent: () => mocks.available,
}));
vi.mock("../models/Response", () => ({
  shoulderTap: mocks.shoulderTap,
}));
vi.mock("../models/request-error", () => ({ describeRequestError: (e: unknown) => String(e) }));

import { renderQueuedMessages } from "./QueuedMessageView";

type AnyVnode = { tag?: unknown; attrs?: Record<string, unknown>; children?: unknown; text?: unknown };

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

function renderedText(node: unknown): string {
  return flatten(node)
    .map((vnode) =>
      typeof vnode.text === "string" ? vnode.text : typeof vnode.children === "string" ? vnode.children : "",
    )
    .join(" ");
}

function findByClass(node: unknown, className: string): AnyVnode | undefined {
  return flatten(node).find((vnode) => {
    const attrs = vnode.attrs ?? {};
    return [attrs.class, attrs.className].some((v) => typeof v === "string" && v.includes(className));
  });
}

function allByClass(node: unknown, className: string): AnyVnode[] {
  return flatten(node).filter((vnode) => {
    const attrs = vnode.attrs ?? {};
    return [attrs.class, attrs.className].some((v) => typeof v === "string" && v.includes(className));
  });
}

function queuedMessage(queued_id: string, content: string): QueuedMessage {
  return { queued_id, content, timestamp: "2026-08-07T00:00:00.000Z" };
}

describe("renderQueuedMessages", () => {
  beforeEach(() => {
    mocks.queued = [];
    mocks.available = true;
    mocks.shoulderTap.mockClear();
  });

  it("renders nothing when the queue is empty", () => {
    expect(renderQueuedMessages("agent-1")).toEqual([]);
  });

  it("renders a header row with the label and the shoulder-tap button plus one bubble per message", () => {
    mocks.queued = [queuedMessage("q1", "first"), queuedMessage("q2", "second")];
    const nodes = renderQueuedMessages("agent-1");
    expect(nodes).toHaveLength(1);
    const text = renderedText(nodes);
    // Header label on the left, shoulder-tap on the right; NO interrupt button.
    expect(text).toContain("Queued messages");
    expect(text).toContain("Shoulder tap");
    expect(text).not.toContain("Interrupt");
    // Both queued messages render verbatim, one bubble each.
    expect(text).toContain("first");
    expect(text).toContain("second");
    expect(allByClass(nodes, "queued-message")).toHaveLength(2);
  });

  it("gives the shoulder-tap button the exact hover tooltip text", () => {
    mocks.queued = [queuedMessage("q1", "hi")];
    const button = findByClass(renderQueuedMessages("agent-1"), "queued-action--flush");
    expect(button?.attrs?.["aria-label"]).toBe("Gently interrupt your agent to send queued messages early");
  });

  it("shows an info affordance next to the label with the explanatory tooltip", () => {
    mocks.queued = [queuedMessage("q1", "hi")];
    const info = findByClass(renderQueuedMessages("agent-info"), "queued-info");
    expect(info?.attrs?.["aria-label"]).toBe(
      "Messages below are sent when your agent takes a breather mid-work or finishes a turn.",
    );
  });

  it("fires the one harness-agnostic shoulder-tap intent on click (no harness branch)", async () => {
    mocks.queued = [queuedMessage("q1", "hi")];
    const button = findByClass(renderQueuedMessages("agent-tap"), "queued-action--flush");
    await (button?.attrs?.onclick as () => Promise<void>)();
    expect(mocks.shoulderTap).toHaveBeenCalledWith("agent-tap");
  });

  it("renders the live backend snapshot during a tap -- no local freeze or reconstruction", async () => {
    const agent = "agent-live-snapshot";
    mocks.queued = [queuedMessage("q1", "hi")];
    let resolveTap: () => void = () => {};
    mocks.shoulderTap.mockImplementationOnce(
      () => new Promise<{ status: string }>((resolve) => (resolveTap = () => resolve({ status: "tapped" }))),
    );

    const button = findByClass(renderQueuedMessages(agent), "queued-action--flush");
    const pending = (button?.attrs?.onclick as () => Promise<void>)();

    // No frozen group is ever painted; the frontend mirrors the backend snapshot.
    expect(findByClass(renderQueuedMessages(agent), "queued-group--frozen")).toBeUndefined();
    // When the backend snapshot empties, the group empties with it -- nothing held back.
    mocks.queued = [];
    expect(renderQueuedMessages(agent)).toEqual([]);

    resolveTap();
    await pending;
    expect(mocks.shoulderTap).toHaveBeenCalledWith(agent);
  });

  it("greys the button when the backend reports the tap unavailable", () => {
    // A non-empty queue still renders the group, but availability=false greys the button --
    // the frontend computes nothing, it just obeys the backend flag.
    mocks.queued = [queuedMessage("q1", "hi")];
    mocks.available = false;
    expect(findByClass(renderQueuedMessages("agent-unavail"), "queued-action--flush")?.attrs?.disabled).toBe(true);
  });

  it("greys the button while this tap's own request is in flight, then re-enables it", async () => {
    const agent = "agent-inflight-tap";
    mocks.queued = [queuedMessage("q1", "hi")];
    mocks.available = true;
    // Available and nothing in flight -> the button is live.
    expect(findByClass(renderQueuedMessages(agent), "queued-action--flush")?.attrs?.disabled).toBe(false);

    let resolveTap: () => void = () => {};
    mocks.shoulderTap.mockImplementationOnce(
      () => new Promise<{ status: string }>((resolve) => (resolveTap = () => resolve({ status: "tapped" }))),
    );
    const button = findByClass(renderQueuedMessages(agent), "queued-action--flush");
    const pending = (button?.attrs?.onclick as () => Promise<void>)();

    // The tap is running -> the button greys so it cannot double-fire.
    expect(findByClass(renderQueuedMessages(agent), "queued-action--flush")?.attrs?.disabled).toBe(true);

    resolveTap();
    await pending;
    // Settled -> the button is live again.
    expect(findByClass(renderQueuedMessages(agent), "queued-action--flush")?.attrs?.disabled).toBe(false);
  });
});
