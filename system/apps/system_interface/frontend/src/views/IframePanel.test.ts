// @vitest-environment jsdom
/**
 * The shell side of the app contract as IframePanel drives it: a page is told who it is on
 * every load of its frame, told again when the tab or view showing it changes, and told
 * shown or hidden only when the pane's visibility changes.
 */
import "../testing/dom";

import { beforeEach, describe, expect, it, vi } from "vitest";

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
        appName: "chat",
        address: ADDRESS,
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

  it("does not re-greet a page no tab shows, and greets it again when one does", () => {
    const { frame, sent } = mountPanel();
    frame.dispatchEvent(new Event("load"));
    sent.mockClear();

    // The page's tab closed, or the active view does not include it: the surface is unbound.
    contract.tabId = "";
    contract.viewId = "project-1";
    contract.isVisible = false;
    m.redraw.sync();
    expect(sentTypes(sent)).toEqual([SHELL_HIDDEN]);

    // A tab in the new view picks the page up again.
    contract.tabId = "chat-agent-1-in-project-1";
    contract.isVisible = true;
    m.redraw.sync();
    expect(sentTypes(sent)).toEqual([SHELL_HIDDEN, SHELL_HANDSHAKE, SHELL_SHOWN]);
    expect(lastHandshake(sent)).toMatchObject({ viewId: "project-1", tabId: "chat-agent-1-in-project-1" });
  });

  it("tells a page nothing before it has loaded", () => {
    const { sent } = mountPanel();
    contract.viewId = "project-1";
    contract.isVisible = false;
    m.redraw.sync();
    expect(sent).not.toHaveBeenCalled();
  });
});

describe("IframePanel's frame url", () => {
  function mountWithUrl(): { frame: () => HTMLIFrameElement; attrs: { url: string; isPageAtUrl: boolean } } {
    const attrs = { url: "http://files.example/", isPageAtUrl: false };
    const root = document.createElement("div");
    document.body.appendChild(root);
    m.mount(root, {
      view: () =>
        m(IframePanel, {
          url: attrs.url,
          isPageAtUrl: attrs.isPageAtUrl,
          title: "Files",
          appName: "files",
          address: "app:files?instance=files-1",
          contract: { ...contract, address: "app:files?instance=files-1" },
        }),
    });
    return {
      attrs,
      frame: () => {
        const frame = root.querySelector("iframe");
        if (frame === null) throw new Error("the panel mounted no iframe");
        return frame;
      },
    };
  }

  it("navigates the frame to a new url, but adopts one the page is already at", () => {
    const { frame, attrs } = mountWithUrl();
    expect(frame().getAttribute("src")).toBe("http://files.example/");

    attrs.url = "http://files.example/notes/";
    attrs.isPageAtUrl = true;
    m.redraw.sync();
    expect(frame().getAttribute("src")).toBe("http://files.example/");

    attrs.url = "http://files.example/elsewhere/";
    attrs.isPageAtUrl = false;
    m.redraw.sync();
    expect(frame().getAttribute("src")).toBe("http://files.example/elsewhere/");

    // A redraw with nothing new never touches the src.
    m.redraw.sync();
    expect(frame().getAttribute("src")).toBe("http://files.example/elsewhere/");
  });
});
