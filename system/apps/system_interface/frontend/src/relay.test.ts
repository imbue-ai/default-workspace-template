// @vitest-environment jsdom
//
// The child-frame boundary is about what a real browser event carries -- an origin, a
// source window, a payload -- so these tests dispatch real MessageEvents at a real (jsdom)
// window with real iframes for the panes, and stand in the chrome as a spy parent.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  initEmbedderRelay,
  isWorkspaceFamilyOrigin,
  resetEmbedderRelayForTesting,
  sendToChildFrame,
  setChildFrameMessageHandler,
} from "./relay";

// jsdom's location is http://localhost:3000, which carries no workspace coordinate, so the
// origin family here is that one host; the coordinate-bearing cases are unit-tested on
// isWorkspaceFamilyOrigin directly.
const SHELL_ORIGIN = window.location.origin;
const FOREIGN_ORIGIN = "https://evil.example";

function mountFrame(): HTMLIFrameElement {
  const iframe = document.createElement("iframe");
  iframe.setAttribute("data-live-key", "chat:agent-1");
  document.body.appendChild(iframe);
  return iframe;
}

function frameWindow(frame: HTMLIFrameElement): Window {
  const paneWindow = frame.contentWindow;
  if (paneWindow === null) throw new Error("jsdom gave the pane iframe no contentWindow");
  return paneWindow;
}

function post(data: unknown, origin: string, source: unknown): void {
  window.dispatchEvent(new MessageEvent("message", { data, origin, source: source as Window }));
}

function framedUnderChrome(): { postMessage: ReturnType<typeof vi.fn> } {
  const chrome = { postMessage: vi.fn() };
  Object.defineProperty(window, "parent", { value: chrome, configurable: true });
  return chrome;
}

beforeEach(() => {
  resetEmbedderRelayForTesting();
  initEmbedderRelay();
});

afterEach(() => {
  resetEmbedderRelayForTesting();
  document.body.innerHTML = "";
  Object.defineProperty(window, "parent", { value: window, configurable: true });
});

describe("isWorkspaceFamilyOrigin", () => {
  it("accepts an origin on the same workspace coordinate and refuses every other", () => {
    const shellHost = "system_interface-x7k9q2w1.host-0123456789abcdef0123456789abcdef.localhost:8421";
    expect(
      isWorkspaceFamilyOrigin("http://chat-ab12cd34.host-0123456789abcdef0123456789abcdef.localhost:8421", shellHost),
    ).toBe(true);
    expect(
      isWorkspaceFamilyOrigin("http://chat-ab12cd34.host-ffffffffffffffffffffffffffffffff.localhost:8421", shellHost),
    ).toBe(false);
    expect(isWorkspaceFamilyOrigin("https://evil.example", shellHost)).toBe(false);
    expect(isWorkspaceFamilyOrigin("null", shellHost)).toBe(false);
    expect(isWorkspaceFamilyOrigin("", shellHost)).toBe(false);
  });

  it("treats a host with no coordinate as its own family", () => {
    expect(isWorkspaceFamilyOrigin("http://127.0.0.1:8000", "127.0.0.1:8000")).toBe(true);
    expect(isWorkspaceFamilyOrigin("http://127.0.0.1:9000", "127.0.0.1:8000")).toBe(false);
  });
});

describe("the embedder relay", () => {
  it("forwards a minds: message from a child frame up to the chrome unchanged", () => {
    const chrome = framedUnderChrome();
    const frame = mountFrame();
    const message = { type: "minds:open-request-modal", requestId: "evt-1", extra: { nested: true } };

    post(message, SHELL_ORIGIN, frameWindow(frame));

    expect(chrome.postMessage).toHaveBeenCalledTimes(1);
    expect(chrome.postMessage).toHaveBeenCalledWith(message, "*");
  });

  it("drops a minds: message from a foreign origin, a stranger window, or a non-minds type", () => {
    const chrome = framedUnderChrome();
    const frame = mountFrame();

    post({ type: "minds:open-help" }, FOREIGN_ORIGIN, frameWindow(frame));
    post({ type: "minds:open-help" }, SHELL_ORIGIN, {});
    post({ type: "ttyd-focus" }, SHELL_ORIGIN, frameWindow(frame));
    post("minds:open-help", SHELL_ORIGIN, frameWindow(frame));

    expect(chrome.postMessage).not.toHaveBeenCalled();
  });

  it("rebroadcasts a chrome message to every child frame unchanged", () => {
    const chrome = framedUnderChrome();
    const first = mountFrame();
    const second = mountFrame();
    const firstSpy = vi.spyOn(frameWindow(first), "postMessage");
    const secondSpy = vi.spyOn(frameWindow(second), "postMessage");
    const verdicts = {
      type: "minds:permission-resolutions",
      resolutions: [{ requestId: "evt-1", resolution: "granted" }],
    };

    post(verdicts, "https://chrome.example", chrome);

    expect(firstSpy).toHaveBeenCalledWith(verdicts, "*");
    expect(secondSpy).toHaveBeenCalledWith(verdicts, "*");
  });

  it("forwards nothing upward on a top-level page", () => {
    const frame = mountFrame();
    // window.parent === window here; a post to it would come straight back as a message
    // from the shell's own window, which must not be mistaken for the chrome.
    const selfSpy = vi.spyOn(window, "postMessage");
    post({ type: "minds:open-help" }, SHELL_ORIGIN, frameWindow(frame));
    expect(selfSpy).not.toHaveBeenCalled();
  });
});

describe("the shell side of the app contract", () => {
  it("dispatches a shell: message to its handler with the posting frame", () => {
    const frame = mountFrame();
    const handler = vi.fn();
    setChildFrameMessageHandler("shell:open", handler);

    post({ type: "shell:open", address: "app:chat?instance=agent-2" }, SHELL_ORIGIN, frameWindow(frame));

    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler.mock.calls[0][0]).toBe(frame);
    expect(handler.mock.calls[0][1]).toEqual({ type: "shell:open", address: "app:chat?instance=agent-2" });
  });

  it("ignores a shell: message from a foreign origin or an unknown window", () => {
    const frame = mountFrame();
    const handler = vi.fn();
    setChildFrameMessageHandler("shell:focused", handler);

    post({ type: "shell:focused" }, FOREIGN_ORIGIN, frameWindow(frame));
    post({ type: "shell:focused" }, SHELL_ORIGIN, {});

    expect(handler).not.toHaveBeenCalled();
  });

  it("sends a contract message to one frame", () => {
    const frame = mountFrame();
    const spy = vi.spyOn(frameWindow(frame), "postMessage");
    sendToChildFrame(frame, "shell:handshake", { clientId: "client-1", tabId: "tab-1" });
    expect(spy).toHaveBeenCalledWith({ type: "shell:handshake", clientId: "client-1", tabId: "tab-1" }, "*");
  });
});
