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
    flushQueue: vi.fn(async () => {}),
  };
});

vi.mock("../models/AgentManager", () => ({
  getQueuedMessagesForAgent: () => mocks.queued,
}));
vi.mock("../models/Response", () => ({
  flushQueue: mocks.flushQueue,
}));
vi.mock("../models/request-error", () => ({ describeRequestError: (e: unknown) => String(e) }));

import { renderQueuedMessages } from "./QueuedMessageView";
import { noteBackendArrivals } from "../models/OutgoingMessages";

type AnyVnode = { tag?: unknown; attrs?: Record<string, unknown>; children?: unknown; text?: unknown };

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
    mocks.flushQueue.mockClear();
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
    expect(button?.attrs?.["data-tooltip"]).toBe("Gently interrupt your agent to send queued messages early");
  });

  it("shows an info affordance next to the label with the explanatory tooltip", () => {
    mocks.queued = [queuedMessage("q1", "hi")];
    const info = findByClass(renderQueuedMessages("agent-info"), "queued-info");
    expect(info?.attrs?.["data-tooltip"]).toBe(
      "Messages below are sent when your agent takes a breather mid-work or finishes a turn.",
    );
  });

  it("fires the flush intent when Shoulder tap is clicked", async () => {
    mocks.queued = [queuedMessage("q1", "hi")];
    const button = findByClass(renderQueuedMessages("agent-1"), "queued-action--flush");
    await (button?.attrs?.onclick as () => Promise<void>)();
    expect(mocks.flushQueue).toHaveBeenCalledWith("agent-1");
  });

  it("freezes the queued group during the flush and releases it on a backend arrival (no blip, no countdown)", async () => {
    // Its own agent id so the module-level freeze state cannot collide with others.
    const agent = "agent-freeze";
    mocks.queued = [queuedMessage("q1", "hi")];
    let resolveFlush: () => void = () => {};
    mocks.flushQueue.mockImplementationOnce(() => new Promise<void>((resolve) => (resolveFlush = resolve)));

    const button = findByClass(renderQueuedMessages(agent), "queued-action--flush");
    const pending = (button?.attrs?.onclick as () => Promise<void>)();

    // In flight: the group is frozen -- the captured message is still shown, the
    // button is gone, and there is NO countdown.
    const duringFlight = renderQueuedMessages(agent);
    expect(findByClass(duringFlight, "queued-group--frozen")).toBeTruthy();
    expect(findByClass(duringFlight, "queued-action--flush")).toBeUndefined();
    expect(findByClass(duringFlight, "queued-countdown")).toBeUndefined();
    expect(renderedText(duringFlight)).toContain("hi");

    // Even when the backend snapshot empties during the restart, the frozen group
    // holds the messages rather than blipping them out.
    mocks.queued = [];
    expect(findByClass(renderQueuedMessages(agent), "queued-group--frozen")).toBeTruthy();

    // The flush POST resolving does NOT release the hold -- that would clear it
    // before the resent turn renders, reopening the blip.
    resolveFlush();
    await pending;
    expect(findByClass(renderQueuedMessages(agent), "queued-group--frozen")).toBeTruthy();

    // A genuinely-new backend arrival (the resent message landing) releases it, so
    // the group hands off to the real (now empty) state exactly as it appears.
    noteBackendArrivals(agent, ["resent-arrival-id"]);
    expect(renderQueuedMessages(agent)).toEqual([]);
    expect(mocks.flushQueue).toHaveBeenCalledWith(agent);
  });
});
