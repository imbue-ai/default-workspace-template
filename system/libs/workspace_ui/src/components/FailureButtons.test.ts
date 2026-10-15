// @vitest-environment jsdom
//
// The failure buttons over a real connection: the shell is a spy parent whose handshakes arrive as real
// MessageEvents, and a press is read off what the page posted to it.

import "../testing/dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import { SHELL_HANDSHAKE, SHELL_REPORT_BUG, SHELL_START_WITH_TEXT, connectToShell } from "../app_contract";
import type { ShellConnection } from "../app_contract";
import { AskAgentToFixButton, ReportBugButton } from "./FailureButtons";

const HANDSHAKE = { type: SHELL_HANDSHAKE, clientId: "client-1", windowId: "win-1", desktopId: "home", app: "docs" };
const FAILURE = { whatFailed: "Docs couldn't open that", details: "HTTP 502\nboom" };

let connection: ShellConnection | null = null;
let root: HTMLElement;

function framed(): { postMessage: ReturnType<typeof vi.fn> } {
  const parent = { postMessage: vi.fn() };
  Object.defineProperty(window, "parent", { value: parent, configurable: true });
  return parent;
}

function handshake(parent: unknown, extra: Record<string, unknown> = {}): void {
  window.dispatchEvent(new MessageEvent("message", { data: { ...HANDSHAKE, ...extra }, source: parent as Window }));
}

function render(view: m.Children): HTMLButtonElement | null {
  m.render(root, view);
  return root.querySelector("button");
}

afterEach(() => {
  connection?.disconnect();
  connection = null;
  Object.defineProperty(window, "parent", { value: window, configurable: true });
});

describe("Ask an agent to fix this", () => {
  it("renders nothing until a shell frames the page, then starts a chat with the failure on press", () => {
    root = document.createElement("div");
    expect(render(m(AskAgentToFixButton, { connection: null, failure: FAILURE }))).toBeNull();

    const parent = framed();
    const live = connectToShell({});
    connection = live;
    const view = (): m.Children => m(AskAgentToFixButton, { connection: live, failure: FAILURE });
    expect(render(view())).toBeNull();

    handshake(parent);
    const button = render(view());
    expect(button?.textContent).toBe("Ask an agent to fix this");
    button?.click();
    const [message] = parent.postMessage.mock.lastCall as [Record<string, unknown>, string];
    expect(message.type).toBe(SHELL_START_WITH_TEXT);
    expect(message.text).toContain("HTTP 502\nboom");
  });

  it("renders nothing on a page opened on its own", () => {
    root = document.createElement("div");
    connection = connectToShell({});
    expect(render(m(AskAgentToFixButton, { connection, failure: FAILURE }))).toBeNull();
  });
});

describe("Report a bug", () => {
  it("appears once the shell's handshake says Imbue Studio opens a bug report, and asks the shell on press", () => {
    root = document.createElement("div");
    const parent = framed();
    const live = connectToShell({});
    connection = live;
    const view = (): m.Children => m(ReportBugButton, { connection: live });

    handshake(parent);
    expect(render(view())).toBeNull();

    // The capabilities reached the shell after this page loaded: it hands the page a fresh handshake.
    handshake(parent, { canReportBug: true });
    const button = render(view());
    expect(button?.textContent).toBe("Report a bug");
    button?.click();
    expect(parent.postMessage).toHaveBeenLastCalledWith({ type: SHELL_REPORT_BUG }, "*");

    handshake(parent, { canReportBug: false });
    expect(render(view())).toBeNull();
  });
});
