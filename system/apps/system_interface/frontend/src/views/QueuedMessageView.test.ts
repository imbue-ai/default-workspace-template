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
    shoulderTapAtomic: vi.fn(async () => ({ status: "tapped" })),
    // Whether the current agent's harness supports the atomic shoulder tap (codex / pi / claude).
    atomicFlag: false,
  };
});

vi.mock("../models/AgentManager", () => ({
  getQueuedMessagesForAgent: () => mocks.queued,
  getAgentById: () => ({ harness: "test-harness" }),
}));
vi.mock("../models/HarnessCatalog", () => ({
  getHarnessCatalog: () => ({ native_atomic_shoulder_tap_possible: mocks.atomicFlag }),
}));
vi.mock("../models/Response", () => ({
  flushQueue: mocks.flushQueue,
  shoulderTapAtomic: mocks.shoulderTapAtomic,
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
    mocks.atomicFlag = false;
    mocks.flushQueue.mockClear();
    mocks.shoulderTapAtomic.mockClear();
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

  it("fires the restart-based flush intent when the harness lacks atomic shoulder tap", async () => {
    // Distinct agent id per flush test: the freeze it leaves is module-level and
    // keyed by agent id, so reusing one would hide the next test's button.
    mocks.atomicFlag = false;
    mocks.queued = [queuedMessage("q1", "hi")];
    const button = findByClass(renderQueuedMessages("agent-restart"), "queued-action--flush");
    await (button?.attrs?.onclick as () => Promise<void>)();
    expect(mocks.flushQueue).toHaveBeenCalledWith("agent-restart");
    expect(mocks.shoulderTapAtomic).not.toHaveBeenCalled();
  });

  it("fires the atomic shoulder-tap intent when the harness supports it (codex)", async () => {
    mocks.atomicFlag = true;
    mocks.queued = [queuedMessage("q1", "hi")];
    const button = findByClass(renderQueuedMessages("agent-atomic"), "queued-action--flush");
    await (button?.attrs?.onclick as () => Promise<void>)();
    expect(mocks.shoulderTapAtomic).toHaveBeenCalledWith("agent-atomic");
    expect(mocks.flushQueue).not.toHaveBeenCalled();
  });

  it("releases the freeze immediately on a terminal no-op atomic status (no hang to the cap)", async () => {
    const agent = "agent-noop-tap";
    mocks.atomicFlag = true;
    mocks.queued = [queuedMessage("q1", "hi")];
    mocks.shoulderTapAtomic.mockResolvedValueOnce({ status: "no_open_turn" });

    const button = findByClass(renderQueuedMessages(agent), "queued-action--flush");
    await (button?.attrs?.onclick as () => Promise<void>)();

    // Nothing was committed, so the freeze is dropped now rather than held to the 20s cap.
    expect(findByClass(renderQueuedMessages(agent), "queued-group--frozen")).toBeUndefined();
  });

  it("keeps the freeze arrival-released on a real ``tapped`` atomic status", async () => {
    const agent = "agent-tapped";
    mocks.atomicFlag = true;
    mocks.queued = [queuedMessage("q1", "hi")];
    mocks.shoulderTapAtomic.mockResolvedValueOnce({ status: "tapped" });

    const button = findByClass(renderQueuedMessages(agent), "queued-action--flush");
    await (button?.attrs?.onclick as () => Promise<void>)();

    // A real tap commits a merged turn, so the freeze stays until that turn arrives.
    mocks.queued = [];
    expect(findByClass(renderQueuedMessages(agent), "queued-group--frozen")).toBeTruthy();
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
