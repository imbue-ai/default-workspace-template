// @vitest-environment jsdom
/**
 * The chat root's relay: which of the inner chat page's messages go up to the shell (the
 * ``minds:`` messages, ``shell:focused``, ``shell:open`` of a sub-agent view), which one the root
 * answers itself (``shell:open`` of a root path, by selecting the chat), what it says of its own
 * from what its pages say (``shell:agent-working``), and that only a message from one of the
 * root's own inner frames counts.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { isForwardedToInnerFrames, isForwardedToShell, rootOpenDecision, startInnerFrameRelay } from "./relay";

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
  vi.restoreAllMocks();
});

describe("isForwardedToShell", () => {
  it("passes the Imbue Studio messages and the shell messages the root re-posts as its own", () => {
    expect(isForwardedToShell({ type: "minds:ready" })).toBe(true);
    expect(isForwardedToShell({ type: "shell:focused" })).toBe(true);
    expect(isForwardedToShell({ type: "shell:input" })).toBe(true);
    expect(isForwardedToShell({ type: "shell:open", path: "/agent-1.agent-2.sess-3", ifPresent: "focus" })).toBe(true);
    expect(isForwardedToShell({ type: "shell:draft-text", text: "Explain this element:" })).toBe(true);
    expect(
      isForwardedToShell({
        type: "shell:show-coach-card",
        text: "Your connections live here.",
        chromeAnchor: "permissions",
      }),
    ).toBe(true);
    expect(
      isForwardedToShell({ type: "shell:message", message: { type: "open:file", path: "/home/user/a.md" } }),
    ).toBe(true);
    expect(isForwardedToShell({ type: "shell:open-link", url: "http://files-ab12cd34.host-0123.localhost/" })).toBe(
      true,
    );
    expect(isForwardedToShell({ type: "shell:report-bug" })).toBe(true);
  });

  it("keeps everything else the page posts, and anything that is not a typed message, at the root", () => {
    expect(isForwardedToShell({ type: "shell:location", path: "/agent-1", title: "Plan" })).toBe(false);
    expect(isForwardedToShell({ type: "shell:capabilities", navigation: false })).toBe(false);
    // Kept, and answered with the root's own word for all its pages.
    expect(isForwardedToShell({ type: "shell:agent-working", isWorking: true })).toBe(false);
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
      () => [],
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
      () => [],
      (chatId) => void selected.push(chatId),
    );

    deliver({ type: "shell:open", path: "/?chat=agent-2", ifPresent: "focus" }, inner);
    deliver({ type: "shell:open", path: "/?chat=agent-3", ifPresent: "focus" }, stranger);
    Object.defineProperty(window, "parent", { value: window, configurable: true });
    deliver({ type: "shell:open", path: "/?chat=agent-4", ifPresent: "focus" }, inner);

    expect(selected).toEqual(["agent-2", "agent-4"]);
    expect(parent.postMessage).not.toHaveBeenCalled();
  });
});

describe("what the root tells the shell of an agent working in its pages", () => {
  const WORKING = [{ type: "shell:agent-working", isWorking: true }, "*"];
  const STOPPED = [{ type: "shell:agent-working", isWorking: false }, "*"];

  /** A root holding two chat pages, and a window that is none of its own. */
  function rootWithTwoPages() {
    const parent = framed();
    const pageA = { name: "page-a" } as unknown as MessageEventSource;
    const pageB = { name: "page-b" } as unknown as MessageEventSource;
    const stranger = { name: "stranger" };
    const relay = startInnerFrameRelay(
      (source) => source === pageA || source === pageB,
      () => [],
      () => undefined,
    );
    const says = (page: unknown, isWorking: unknown): void =>
      deliver({ type: "shell:agent-working", isWorking }, page);
    return { parent, pageA, pageB, stranger, relay, says };
  }

  it("says an agent is working from a page's start to its end, in its own words and only on a change", () => {
    const { parent, pageA, stranger, says } = rootWithTwoPages();

    says(pageA, false);
    says(stranger, true);
    expect(parent.postMessage).not.toHaveBeenCalled();

    says(pageA, true);
    says(pageA, true);
    says(pageA, false);

    expect(parent.postMessage.mock.calls).toEqual([WORKING, STOPPED]);
  });

  it("keeps saying so when one page finishes while another still has an agent working", () => {
    const { parent, pageA, pageB, says } = rootWithTwoPages();

    says(pageA, true);
    says(pageB, true);
    says(pageB, false);
    expect(parent.postMessage.mock.calls).toEqual([WORKING]);

    says(pageA, false);
    expect(parent.postMessage.mock.calls).toEqual([WORKING, STOPPED]);
  });

  it("leaves no stale word behind for a page that goes away while its agent works", () => {
    const { parent, pageA, pageB, relay, says } = rootWithTwoPages();

    says(pageA, true);
    says(pageB, true);
    relay.forgetAgentWorking(pageA);
    expect(parent.postMessage.mock.calls).toEqual([WORKING]);

    // The last page saying so is evicted mid-turn: nothing is left to say it for.
    relay.forgetAgentWorking(pageB);
    expect(parent.postMessage.mock.calls).toEqual([WORKING, STOPPED]);
  });

  it("says so again to a shell that greets the root while an agent works, and not otherwise", () => {
    const { parent, pageA, relay, says } = rootWithTwoPages();

    relay.repeatAgentWorking();
    expect(parent.postMessage).not.toHaveBeenCalled();

    says(pageA, true);
    // The page answers the handshake the root hands it by saying so again, which changes nothing here.
    says(pageA, true);
    relay.repeatAgentWorking();
    expect(parent.postMessage.mock.calls).toEqual([WORKING, WORKING]);

    says(pageA, false);
    relay.repeatAgentWorking();
    expect(parent.postMessage.mock.calls).toEqual([WORKING, WORKING, STOPPED]);
  });

  it("drops a page's word that carries no boolean, forwarding nothing", () => {
    const { parent, pageA, says } = rootWithTwoPages();

    says(pageA, "true");
    says(pageA, undefined);
    says(pageA, 1);

    expect(parent.postMessage).not.toHaveBeenCalled();
  });

  it("tells nobody from a root opened on its own", () => {
    const pageA = { name: "page-a" } as unknown as MessageEventSource;
    const post = vi.spyOn(window, "postMessage");
    const relay = startInnerFrameRelay(
      (source) => source === pageA,
      () => [],
      () => undefined,
    );

    deliver({ type: "shell:agent-working", isWorking: true }, pageA);
    relay.repeatAgentWorking();

    expect(post).not.toHaveBeenCalled();
  });
});

describe("passing the chrome's answers down", () => {
  it("passes only the chrome's answers to an inner page's asks, and only from the parent", () => {
    expect(isForwardedToInnerFrames({ type: "minds:provider-sign-in-ack", relay: true })).toBe(true);
    expect(
      isForwardedToInnerFrames({
        type: "minds:permission-resolutions",
        resolutions: [{ requestId: "req-1", resolution: "granted" }],
      }),
    ).toBe(true);
    expect(isForwardedToInnerFrames({ type: "minds:focus-chat", chatId: "agent-1" })).toBe(false);
    expect(isForwardedToInnerFrames(null)).toBe(false);
  });

  it("hands a permission verdict from the shell to every inner page, so a card flips and the strip covers the wait", () => {
    const parent = framed();
    const pageA = { postMessage: vi.fn() };
    const pageB = { postMessage: vi.fn() };
    startInnerFrameRelay(
      () => false,
      () => [pageA, pageB] as unknown as Window[],
      () => undefined,
    );
    const verdict = {
      type: "minds:permission-resolutions",
      resolutions: [{ requestId: "req-1", resolution: "granted" }],
    };

    deliver(verdict, parent);
    deliver(verdict, { name: "stranger" });

    expect(pageA.postMessage.mock.calls).toEqual([[verdict, "*"]]);
    expect(pageB.postMessage.mock.calls).toEqual([[verdict, "*"]]);
    expect(parent.postMessage).not.toHaveBeenCalled();
  });

  it("hands the sign-in answer from the shell to every inner page, and ignores it from anyone else", () => {
    const parent = framed();
    const pageA = { postMessage: vi.fn() };
    const pageB = { postMessage: vi.fn() };
    const inner = { name: "inner" };
    startInnerFrameRelay(
      (source) => source === (inner as unknown as MessageEventSource),
      () => [pageA, pageB] as unknown as Window[],
      () => undefined,
    );
    const ack = { type: "minds:provider-sign-in-ack", relay: true };

    deliver(ack, parent);
    deliver({ type: "minds:focus-chat", chatId: "agent-1" }, parent);
    deliver(ack, { name: "stranger" });

    expect(pageA.postMessage.mock.calls).toEqual([[ack, "*"]]);
    expect(pageB.postMessage.mock.calls).toEqual([[ack, "*"]]);
    expect(parent.postMessage).not.toHaveBeenCalled();
  });
});
