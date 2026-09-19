// @vitest-environment jsdom
/**
 * The chat root's relay: which of the inner chat page's messages go up to the shell (the
 * ``minds:`` messages, ``shell:focused``, ``shell:open``), and that only a message from one of
 * the root's own inner frames is forwarded.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { isForwardedToShell, startInnerFrameRelay } from "./relay";

/** Frame this window under a spy parent for the duration of the test. */
function framed(): { postMessage: ReturnType<typeof vi.fn> } {
  const parent = { postMessage: vi.fn() };
  Object.defineProperty(window, "parent", { value: parent, configurable: true });
  return parent;
}

function deliver(data: unknown, source: unknown): void {
  window.dispatchEvent(new MessageEvent("message", { data, source: source as Window }));
}

afterEach(() => {
  Object.defineProperty(window, "parent", { value: window, configurable: true });
});

describe("isForwardedToShell", () => {
  it("passes the minds messages and the two shell messages the root re-posts as its own", () => {
    expect(isForwardedToShell({ type: "minds:ready" })).toBe(true);
    expect(isForwardedToShell({ type: "shell:focused" })).toBe(true);
    expect(isForwardedToShell({ type: "shell:open", path: "/agent-1.agent-2.sess-3" })).toBe(true);
  });

  it("keeps everything else the page posts, and anything that is not a typed message, at the root", () => {
    expect(isForwardedToShell({ type: "shell:location", path: "/agent-1", title: "Plan" })).toBe(false);
    expect(isForwardedToShell({ type: "shell:capabilities", navigation: false })).toBe(false);
    expect(isForwardedToShell({ type: 7 })).toBe(false);
    expect(isForwardedToShell("minds:ready")).toBe(false);
    expect(isForwardedToShell(null)).toBe(false);
  });
});

describe("startInnerFrameRelay", () => {
  it("re-posts a forwarded message from an inner frame to the parent, and drops every other one", () => {
    const parent = framed();
    const inner = { name: "inner" };
    const stranger = { name: "stranger" };
    startInnerFrameRelay((source) => source === (inner as unknown as MessageEventSource));

    deliver({ type: "minds:ready" }, inner);
    deliver({ type: "shell:location", path: "/agent-1", title: "" }, inner);
    deliver({ type: "minds:ready" }, stranger);

    expect(parent.postMessage.mock.calls).toEqual([[{ type: "minds:ready" }, "*"]]);
  });
});
