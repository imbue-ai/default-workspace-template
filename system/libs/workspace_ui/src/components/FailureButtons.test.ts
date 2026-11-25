// @vitest-environment jsdom
//
// The Report a bug button over a real connection: the shell is a spy parent whose handshakes arrive as real
// MessageEvents, and a press is read off what the page posted to it.

import "../testing/dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import { SHELL_HANDSHAKE, SHELL_REPORT_BUG, connectToShell } from "../app_contract";
import type { ShellConnection } from "../app_contract";
import { ReportBugButton } from "./FailureButtons";

const HANDSHAKE = { type: SHELL_HANDSHAKE, clientId: "client-1", windowId: "win-1", desktopId: "home", app: "docs" };

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

describe("Report a bug", () => {
  it("appears once the shell's handshake says Imbue Studio opens a bug report, and sends the shell its failure", () => {
    root = document.createElement("div");
    const parent = framed();
    const live = connectToShell({});
    connection = live;
    const failure = {
      message: "Docs couldn't open that. Try again.",
      details: "HTTP 502",
    };
    const view = (): m.Children => m(ReportBugButton, { connection: live, failure });

    handshake(parent);
    expect(render(view())).toBeNull();

    // The capabilities reached the shell after this page loaded: it hands the page a fresh handshake.
    handshake(parent, { canReportBug: true });
    const button = render(view());
    expect(button?.textContent).toBe("Report a bug");
    button?.click();
    expect(parent.postMessage).toHaveBeenLastCalledWith({ type: SHELL_REPORT_BUG, ...failure }, "*");

    handshake(parent, { canReportBug: false });
    expect(render(view())).toBeNull();
  });
});
