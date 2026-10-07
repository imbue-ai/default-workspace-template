// @vitest-environment jsdom
/**
 * The chat root's relay: which of the inner chat page's messages go up to the shell (the
 * ``minds:`` messages, ``shell:focused``, ``shell:open`` of a sub-agent view), which one the root
 * answers itself (``shell:open`` of a root path, by selecting the chat), and that only a message
 * from one of the root's own inner frames counts.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { isForwardedToShell, rootOpenDecision, startInnerFrameRelay } from "./relay";

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
  it("passes the Imbue Studio messages and the shell messages the root re-posts as its own", () => {
    expect(isForwardedToShell({ type: "minds:ready" })).toBe(true);
    expect(isForwardedToShell({ type: "shell:focused" })).toBe(true);
    expect(isForwardedToShell({ type: "shell:open", path: "/agent-1.agent-2.sess-3", ifPresent: "focus" })).toBe(true);
    expect(isForwardedToShell({ type: "shell:draft-text", text: "Explain this element:" })).toBe(true);
    expect(
      isForwardedToShell({ type: "shell:message", message: { type: "open:file", path: "/home/user/a.md" } }),
    ).toBe(true);
    expect(isForwardedToShell({ type: "shell:open-link", url: "http://files-ab12cd34.host-0123.localhost/" })).toBe(
      true,
    );
  });

  it("keeps everything else the page posts, and anything that is not a typed message, at the root", () => {
    expect(isForwardedToShell({ type: "shell:location", path: "/agent-1", title: "Plan" })).toBe(false);
    expect(isForwardedToShell({ type: "shell:capabilities", navigation: false })).toBe(false);
    expect(isForwardedToShell({ type: 7 })).toBe(false);
    expect(isForwardedToShell("minds:ready")).toBe(false);
    expect(isForwardedToShell(null)).toBe(false);
  });
});

describe("rootOpenDecision", () => {
  it("selects the chat a root path names, in place", () => {
    expect(rootOpenDecision({ type: "shell:open", path: "/?chat=agent-2", ifPresent: "focus" })).toEqual({
      kind: "select",
      chatId: "agent-2",
    });
    expect(rootOpenDecision({ type: "shell:open", path: "/", ifPresent: "focus" })).toEqual({
      kind: "select",
      chatId: null,
    });
  });

  it("forwards a sub-agent view's path, and the address form nobody takes any more", () => {
    expect(rootOpenDecision({ type: "shell:open", path: "/agent-1.agent-2.sess-3", ifPresent: "focus" })).toEqual({
      kind: "forward",
    });
    expect(rootOpenDecision({ type: "shell:open", address: "app:chat?instance=agent-2" })).toEqual({
      kind: "forward",
    });
  });

  it("is no decision for anything but an open", () => {
    expect(rootOpenDecision({ type: "shell:focused" })).toEqual({ kind: "not-an-open" });
    expect(rootOpenDecision(null)).toEqual({ kind: "not-an-open" });
  });
});

describe("startInnerFrameRelay", () => {
  it("re-posts a forwarded message from an inner frame to the parent, and drops every other one", () => {
    const parent = framed();
    const inner = { name: "inner" };
    const stranger = { name: "stranger" };
    const selected: (string | null)[] = [];
    startInnerFrameRelay(
      (source) => source === (inner as unknown as MessageEventSource),
      (chatId) => void selected.push(chatId),
    );

    deliver({ type: "minds:ready" }, inner);
    deliver({ type: "shell:location", path: "/agent-1", title: "" }, inner);
    deliver({ type: "minds:ready" }, stranger);
    deliver({ type: "shell:open", path: "/agent-1.agent-2.sess-3", ifPresent: "focus" }, inner);

    expect(parent.postMessage.mock.calls).toEqual([
      [{ type: "minds:ready" }, "*"],
      [{ type: "shell:open", path: "/agent-1.agent-2.sess-3", ifPresent: "focus" }, "*"],
    ]);
    expect(selected).toEqual([]);
  });

  it("selects a sibling chat an inner page asks for instead of forwarding, even unframed", () => {
    const parent = framed();
    const inner = { name: "inner" };
    const stranger = { name: "stranger" };
    const selected: (string | null)[] = [];
    startInnerFrameRelay(
      (source) => source === (inner as unknown as MessageEventSource),
      (chatId) => void selected.push(chatId),
    );

    deliver({ type: "shell:open", path: "/?chat=agent-2", ifPresent: "focus" }, inner);
    deliver({ type: "shell:open", path: "/?chat=agent-3", ifPresent: "focus" }, stranger);
    Object.defineProperty(window, "parent", { value: window, configurable: true });
    deliver({ type: "shell:open", path: "/?chat=agent-4", ifPresent: "focus" }, inner);

    expect(selected).toEqual(["agent-2", "agent-4"]);
    expect(parent.postMessage).not.toHaveBeenCalled();
  });

  it("opens a page's web link in a new tab itself when no shell frames the root, and forwards it when one does", () => {
    const opened = vi.spyOn(window, "open").mockImplementation(() => null);
    const inner = { name: "inner" };
    startInnerFrameRelay(
      (source) => source === (inner as unknown as MessageEventSource),
      () => undefined,
    );

    deliver({ type: "shell:open-link", url: "http://localhost:5173/preview" }, inner);
    deliver({ type: "shell:open-link", url: "file:///home/user/plan.md" }, inner);
    const parent = framed();
    deliver({ type: "shell:open-link", url: "http://localhost:5173/other" }, inner);

    expect(opened.mock.calls).toEqual([["http://localhost:5173/preview", "_blank", "noopener"]]);
    expect(parent.postMessage.mock.calls).toEqual([
      [{ type: "shell:open-link", url: "http://localhost:5173/other" }, "*"],
    ]);
    opened.mockRestore();
  });
});
