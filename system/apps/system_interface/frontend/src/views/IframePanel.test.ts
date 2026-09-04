// @vitest-environment jsdom
/**
 * The shell side of the app contract as IframePanel drives it: a page is told who it is on
 * every load of its frame, told again when the tab or view showing it changes, and told
 * shown or hidden only when the pane's visibility changes.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import m from "mithril";

import { SHELL_HANDSHAKE, SHELL_HIDDEN, SHELL_SHOWN } from "../app_contract";
import { IframePanel } from "./IframePanel";
import type { IframeContractAttrs } from "./IframePanel";

const ADDRESS = "app:chat?instance=agent-1";

/** The panel's contract attrs, mutated between redraws the way the dock's reconcile does. */
const contract: IframeContractAttrs = {
  address: ADDRESS,
  tabId: "chat-agent-1",
  viewId: "everything",
  isVisible: true,
};

function mountPanel(): { frame: HTMLIFrameElement; sent: ReturnType<typeof vi.fn> } {
  const root = document.createElement("div");
  document.body.appendChild(root);
  m.mount(root, {
    view: () =>
      m(IframePanel, {
        url: "http://chat.example/agent-1",
        title: "Chat 1",
        serviceName: "chat",
        liveKey: "chat:agent-1",
        contract,
      }),
  });
  const frame = root.querySelector("iframe");
  if (frame === null || frame.contentWindow === null) throw new Error("the panel mounted no iframe");
  const sent = vi.fn();
  frame.contentWindow.postMessage = sent as unknown as Window["postMessage"];
  return { frame, sent };
}

function sentTypes(sent: ReturnType<typeof vi.fn>): string[] {
  return sent.mock.calls.map(([message]) => (message as { type: string }).type);
}

function lastHandshake(sent: ReturnType<typeof vi.fn>): Record<string, unknown> {
  const handshakes = sent.mock.calls.filter(([message]) => (message as { type: string }).type === SHELL_HANDSHAKE);
  return handshakes[handshakes.length - 1][0] as Record<string, unknown>;
}

beforeEach(() => {
  document.body.innerHTML = "";
  localStorage.setItem("si-client-id", "client-1");
  contract.tabId = "chat-agent-1";
  contract.viewId = "everything";
  contract.isVisible = true;
});

describe("IframePanel's side of the app contract", () => {
  it("hands the page its identity and visibility on every load", () => {
    const { frame, sent } = mountPanel();
    expect(sent).not.toHaveBeenCalled();

    frame.dispatchEvent(new Event("load"));

    expect(sentTypes(sent)).toEqual([SHELL_HANDSHAKE, SHELL_SHOWN]);
    expect(lastHandshake(sent)).toEqual({
      type: SHELL_HANDSHAKE,
      clientId: "client-1",
      deviceKind: "desktop",
      viewId: "everything",
      address: ADDRESS,
      tabId: "chat-agent-1",
    });

    // A reload is a fresh page that has been told nothing.
    frame.dispatchEvent(new Event("load"));
    expect(sentTypes(sent)).toEqual([SHELL_HANDSHAKE, SHELL_SHOWN, SHELL_HANDSHAKE, SHELL_SHOWN]);
  });

  it("sends shown and hidden only when the pane's visibility changes", () => {
    const { frame, sent } = mountPanel();
    frame.dispatchEvent(new Event("load"));
    sent.mockClear();

    m.redraw.sync();
    expect(sent).not.toHaveBeenCalled();

    contract.isVisible = false;
    m.redraw.sync();
    m.redraw.sync();
    expect(sentTypes(sent)).toEqual([SHELL_HIDDEN]);

    contract.isVisible = true;
    m.redraw.sync();
    expect(sentTypes(sent)).toEqual([SHELL_HIDDEN, SHELL_SHOWN]);
  });

  it("hands the page a fresh handshake when the tab or view showing it changes", () => {
    const { frame, sent } = mountPanel();
    frame.dispatchEvent(new Event("load"));
    sent.mockClear();

    // The shell chose its view after the frame loaded, or the user switched views.
    contract.viewId = "project-1";
    m.redraw.sync();
    expect(sentTypes(sent)).toEqual([SHELL_HANDSHAKE]);
    expect(lastHandshake(sent)).toMatchObject({ viewId: "project-1", tabId: "chat-agent-1" });

    // Another view's pane now stands in for the page.
    contract.tabId = "chat-agent-1-in-project-1";
    m.redraw.sync();
    expect(sentTypes(sent)).toEqual([SHELL_HANDSHAKE, SHELL_HANDSHAKE]);
    expect(lastHandshake(sent)).toMatchObject({ viewId: "project-1", tabId: "chat-agent-1-in-project-1" });

    m.redraw.sync();
    expect(sentTypes(sent)).toEqual([SHELL_HANDSHAKE, SHELL_HANDSHAKE]);
  });

  it("tells a page nothing before it has loaded", () => {
    const { sent } = mountPanel();
    contract.viewId = "project-1";
    contract.isVisible = false;
    m.redraw.sync();
    expect(sent).not.toHaveBeenCalled();
  });
});
