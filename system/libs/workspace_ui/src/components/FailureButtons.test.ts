// @vitest-environment jsdom
//
// The failure buttons over a real connection: the shell is a spy parent whose handshakes arrive as real
// MessageEvents, and a press is read off what the page posted to it.

import "../testing/dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import {
  SHELL_HANDSHAKE,
  SHELL_REPORT_BUG,
  SHELL_START_NEW_WITH_TEXT,
  SHELL_START_NEW_WITH_TEXT_RESULT,
  connectToShell,
} from "../app_contract";
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
  it("renders nothing until a shell frames the page, then starts a new chat with the failure on press", () => {
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
    expect(message.type).toBe(SHELL_START_NEW_WITH_TEXT);
    expect(message.text).toContain("HTTP 502\nboom");
  });

  it("reads Asking an agent and stays disabled until the shell answers, then comes back, with the failure if any", async () => {
    root = document.createElement("div");
    const parent = framed();
    const live = connectToShell({});
    connection = live;
    handshake(parent);
    const view = (): m.Children => m(AskAgentToFixButton, { connection: live, failure: FAILURE });
    /** Answer the page's last request, then let the button take the answer. */
    async function answer(reply: Record<string, unknown>): Promise<void> {
      const [message] = parent.postMessage.mock.lastCall as [Record<string, unknown>, string];
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: SHELL_START_NEW_WITH_TEXT_RESULT, requestId: message.requestId, ...reply },
          source: parent as unknown as Window,
        }),
      );
      await new Promise<void>((resolve) => setTimeout(resolve, 0));
    }

    render(view())?.click();
    const pending = render(view());
    expect(pending?.textContent).toBe("Asking an agent…");
    expect(pending?.disabled).toBe(true);
    const sent = parent.postMessage.mock.calls.length;
    pending?.click();
    expect(parent.postMessage.mock.calls.length).toBe(sent);

    await answer({
      isOpened: false,
      failure: { message: "Chat couldn't open that.", details: "HTTP 400\nno account", isReportable: false },
    });
    const failed = render(view());
    expect(failed?.textContent).toBe("Ask an agent to fix this");
    expect(failed?.disabled).toBe(false);
    expect(root.querySelector(".ask-agent-failure")?.textContent).toContain("Chat couldn't open that.");
    expect(root.querySelector(".ask-agent-failure .see-details pre")?.textContent).toBe("HTTP 400\nno account");

    failed?.click();
    expect(root.querySelector(".ask-agent-failure")).not.toBeNull();
    render(view());
    expect(root.querySelector(".ask-agent-failure")).toBeNull();
    await answer({ isOpened: true });
    expect(render(view())?.textContent).toBe("Ask an agent to fix this");
    expect(root.querySelector(".ask-agent-failure")).toBeNull();
  });

  it("offers Report a bug for a reportable failure it shows, where the shell can open one, carrying that failure", async () => {
    root = document.createElement("div");
    const parent = framed();
    const live = connectToShell({});
    connection = live;
    handshake(parent, { canReportBug: true });
    const view = (): m.Children => m(AskAgentToFixButton, { connection: live, failure: FAILURE });
    render(view())?.click();
    const [asked] = parent.postMessage.mock.lastCall as [Record<string, unknown>, string];
    const failure = {
      message: "Chat couldn't open that. Try again. If this persists, report a bug.",
      details: "HTTP 502\nchat answered 500",
      isReportable: true,
    };
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: SHELL_START_NEW_WITH_TEXT_RESULT, requestId: asked.requestId, isOpened: false, failure },
        source: parent as unknown as Window,
      }),
    );
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    m.render(root, view());

    const reportBug = root.querySelector<HTMLButtonElement>(".ask-agent-failure .report-bug");
    expect(reportBug?.textContent).toBe("Report a bug");
    reportBug?.click();
    expect(parent.postMessage).toHaveBeenLastCalledWith(
      { type: SHELL_REPORT_BUG, message: failure.message, details: failure.details },
      "*",
    );

    handshake(parent, { canReportBug: false });
    m.render(root, view());
    expect(root.querySelector(".ask-agent-failure .report-bug")).toBeNull();
  });

  it("takes an answer that comes after a restarted shell settled its request as not answered", async () => {
    root = document.createElement("div");
    const parent = framed();
    const live = connectToShell({});
    connection = live;
    handshake(parent, { shellId: "shell-1" });
    const view = (): m.Children => m(AskAgentToFixButton, { connection: live, failure: FAILURE });
    render(view())?.click();
    const [asked] = parent.postMessage.mock.lastCall as [Record<string, unknown>, string];
    handshake(parent, { shellId: "shell-2" });
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    render(view());
    expect(root.querySelector(".ask-agent-failure-message")?.textContent).toContain("didn't answer");

    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: SHELL_START_NEW_WITH_TEXT_RESULT, requestId: asked.requestId, isOpened: true },
        source: parent as unknown as Window,
      }),
    );
    render(view());
    expect(root.querySelector(".ask-agent-failure")).toBeNull();
  });

  it("renders nothing on a page opened on its own", () => {
    root = document.createElement("div");
    connection = connectToShell({});
    expect(render(m(AskAgentToFixButton, { connection, failure: FAILURE }))).toBeNull();
  });
});

describe("Report a bug", () => {
  it("appears once the shell's handshake says Imbue Studio opens a bug report, and sends the shell its failure", () => {
    root = document.createElement("div");
    const parent = framed();
    const live = connectToShell({});
    connection = live;
    const failure = {
      message: "Docs couldn't open that. Try again. If this persists, report a bug.",
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
