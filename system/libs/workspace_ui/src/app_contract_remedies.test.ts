// @vitest-environment jsdom
//
// ``mountFailureRemedies`` on a real (jsdom) page framed by a stand-in shell: what it draws as the shell greets the
// page and answers its ask, and what it posts to the shell.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CHANGE_NOT_ANSWERED_SENTENCE,
  SHELL_ASK_AGENT,
  SHELL_ASK_AGENT_RESULT,
  SHELL_HANDSHAKE,
  SHELL_REPORT_BUG,
  agentFixTask,
  connectToShell,
  mountFailureRemedies,
  withAskFailure,
} from "./app_contract";
import type { AgentFixRequest, MountedFailureRemedies, ShellConnection, ShownFailure } from "./app_contract";

const HANDSHAKE = { type: SHELL_HANDSHAKE, clientId: "client-1", windowId: "win-1", shellId: "shell-1" };

const PAGE_FAILURE: ShownFailure = {
  message: "Docs isn't available right now.",
  details: "Its built files are missing.",
  isHelpOwed: true,
};
const REQUEST: AgentFixRequest = {
  whatFailed: PAGE_FAILURE.message,
  details: PAGE_FAILURE.details,
  instruction: "Rebuild it, then tell the user what was wrong and what you changed.",
};
const REFUSED = { message: "Chat couldn't open that.", details: "HTTP 500\nboom", isHelpOwed: true };

let connection: ShellConnection | null = null;

afterEach(() => {
  connection?.disconnect();
  connection = null;
  Object.defineProperty(window, "parent", { value: window, configurable: true });
  document.body.replaceChildren();
});

interface Mounted {
  readonly parent: { postMessage: ReturnType<typeof vi.fn> };
  readonly root: HTMLElement;
  readonly remedies: MountedFailureRemedies;
}

/** The remedies for ``failure`` on a page a spy parent frames, redrawn on each handshake as a page does. */
function mounted(failure: ShownFailure = PAGE_FAILURE, askLabel?: string): Mounted {
  const parent = { postMessage: vi.fn() };
  Object.defineProperty(window, "parent", { value: parent, configurable: true });
  const root = document.createElement("div");
  document.body.append(root);
  let remedies: MountedFailureRemedies | null = null;
  connection = connectToShell({ onHandshake: () => remedies?.redraw() });
  remedies = mountFailureRemedies(root, connection, { failure, request: REQUEST, askLabel });
  return { parent, root, remedies };
}

function greet(parent: unknown, fields: Record<string, unknown> = {}): void {
  window.dispatchEvent(new MessageEvent("message", { data: { ...HANDSHAKE, ...fields }, source: parent as Window }));
}

function answer(parent: unknown, fields: Record<string, unknown>): void {
  window.dispatchEvent(
    new MessageEvent("message", { data: { type: SHELL_ASK_AGENT_RESULT, ...fields }, source: parent as Window }),
  );
}

/** The buttons a person can see under ``root``, by their words. */
function shownButtons(root: HTMLElement): string[] {
  return Array.from(root.querySelectorAll("button"))
    .filter((button) => button.closest("[hidden]") === null)
    .map((button) => button.textContent ?? "");
}

function button(root: HTMLElement, label: string): HTMLButtonElement {
  const found = Array.from(root.querySelectorAll("button")).find(
    (candidate) => candidate.textContent === label && candidate.closest("[hidden]") === null,
  );
  if (found === undefined) throw new Error(`no shown button reads ${label}`);
  return found;
}

/** What the page posted of ``type``, in order. */
function posted(parent: Mounted["parent"], type: string): Record<string, unknown>[] {
  return parent.postMessage.mock.calls
    .map(([message]) => message as Record<string, unknown>)
    .filter((message) => message.type === type);
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("mountFailureRemedies", () => {
  it("offers nothing until a shell greets the page, then the ask under the page's own words", () => {
    const { parent, root } = mounted(PAGE_FAILURE, "Ask an agent to rebuild it");
    expect(shownButtons(root)).toEqual([]);
    greet(parent);
    expect(shownButtons(root)).toEqual(["Ask an agent to rebuild it"]);
  });

  it("offers nothing for a failure that owes no help, even with a shell", () => {
    const { parent, root } = mounted({ ...PAGE_FAILURE, isHelpOwed: false });
    greet(parent, { canReportBug: true });
    expect(shownButtons(root)).toEqual([]);
  });

  it("asks with the request's task, says the ask is out, and shows a refused ask with its remedies", async () => {
    const { parent, root } = mounted();
    greet(parent, { canReportBug: true });
    const ask = button(root, "Ask an agent to fix this");
    ask.click();
    ask.click();
    const [asked, ...again] = posted(parent, SHELL_ASK_AGENT);
    expect(again).toEqual([]);
    expect(asked).toMatchObject({ text: agentFixTask(REQUEST), showsOutcome: true });
    expect("delivery" in asked).toBe(false);
    expect(ask.disabled).toBe(true);
    expect(ask.textContent).toBe("Asking an agent…");

    answer(parent, { requestId: asked.requestId, isOpened: false, failure: REFUSED });
    await settle();
    expect(ask.disabled).toBe(false);
    expect(ask.textContent).toBe("Ask an agent to fix this");
    expect(root.querySelector(".ask-agent-failure-message")?.textContent).toBe(REFUSED.message);
    expect(root.querySelector(".see-details-text")?.textContent).toBe(REFUSED.details);
    expect(shownButtons(root)).toEqual(["Ask an agent to fix this", "Draft it in a chat", "Report a bug"]);

    // The draft is the same request, left unsent; the report files the page's failure with the ask's.
    button(root, "Draft it in a chat").click();
    expect(posted(parent, SHELL_ASK_AGENT)[1]).toMatchObject({ text: asked.text, delivery: "draft" });
    button(root, "Report a bug").click();
    const filed = withAskFailure(PAGE_FAILURE, REFUSED);
    expect(posted(parent, SHELL_REPORT_BUG)).toEqual([
      { type: SHELL_REPORT_BUG, message: filed.message, details: filed.details },
    ]);

    // The shell greeting the page to say it can no longer open a bug report takes that offer away.
    greet(parent, { canReportBug: false });
    expect(shownButtons(root)).toEqual(["Ask an agent to fix this", "Draft it in a chat"]);
  });

  it("clears a refused ask once the next one opens the chat", async () => {
    const { parent, root } = mounted();
    greet(parent);
    button(root, "Ask an agent to fix this").click();
    answer(parent, { requestId: posted(parent, SHELL_ASK_AGENT)[0].requestId, isOpened: false, failure: REFUSED });
    await settle();
    button(root, "Ask an agent to fix this").click();
    expect(shownButtons(root)).toEqual(["Asking an agent…"]);
    answer(parent, { requestId: posted(parent, SHELL_ASK_AGENT)[1].requestId, isOpened: true });
    await settle();
    expect(shownButtons(root)).toEqual(["Ask an agent to fix this"]);
    expect(root.querySelector<HTMLElement>(".ask-agent-failure")?.hidden).toBe(true);
  });

  it("offers neither a draft nor a bug report for a refusal that owes no help, nor details it does not have", async () => {
    const { parent, root } = mounted();
    greet(parent, { canReportBug: true });
    button(root, "Ask an agent to fix this").click();
    const failure = { message: "That chat is busy.", details: null, isHelpOwed: false };
    answer(parent, { requestId: posted(parent, SHELL_ASK_AGENT)[0].requestId, isOpened: false, failure });
    await settle();
    expect(root.querySelector(".ask-agent-failure-message")?.textContent).toBe("That chat is busy.");
    expect(root.querySelector<HTMLElement>(".see-details")?.hidden).toBe(true);
    expect(shownButtons(root)).toEqual(["Ask an agent to fix this"]);
  });

  it("shows a restarted shell's ask as not answered, and the old shell's late answer in its place", async () => {
    const { parent, root } = mounted();
    greet(parent);
    button(root, "Ask an agent to fix this").click();
    const [asked] = posted(parent, SHELL_ASK_AGENT);
    greet(parent, { shellId: "shell-2" });
    await settle();
    expect(root.querySelector(".ask-agent-failure-message")?.textContent).toBe(CHANGE_NOT_ANSWERED_SENTENCE);
    // Not answered owes no help: trying again is the whole remedy.
    expect(shownButtons(root)).toEqual(["Ask an agent to fix this"]);

    answer(parent, { requestId: asked.requestId, isOpened: false, failure: REFUSED });
    expect(root.querySelector(".ask-agent-failure-message")?.textContent).toBe(REFUSED.message);
    expect(shownButtons(root)).toEqual(["Ask an agent to fix this", "Draft it in a chat"]);
  });

  it("drops a late answer to an ask pressed again since", async () => {
    const { parent, root } = mounted();
    greet(parent);
    button(root, "Ask an agent to fix this").click();
    const [first] = posted(parent, SHELL_ASK_AGENT);
    greet(parent, { shellId: "shell-2" });
    await settle();
    button(root, "Ask an agent to fix this").click();
    answer(parent, { requestId: first.requestId, isOpened: false, failure: REFUSED });
    expect(shownButtons(root)).toEqual(["Asking an agent…"]);
    expect(root.querySelector<HTMLElement>(".ask-agent-failure")?.hidden).toBe(true);
  });
});
