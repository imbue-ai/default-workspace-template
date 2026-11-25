// @vitest-environment jsdom
/**
 * The chat list's verbs, failed: told in the root's styled notice with the server's text behind See details, never a
 * browser alert; a delete nothing answered keeps its row marked until the list says how it went.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

const verbs = vi.hoisted(() => ({
  stopChat: vi.fn(async (_chatId: string) => undefined),
  startChat: vi.fn(async (_chatId: string) => undefined),
  destroyChat: vi.fn(async (_chatId: string) => undefined),
  renameChat: vi.fn(async (_chatId: string, _title: string) => undefined),
}));
vi.mock("./verbs", () => verbs);

const chatsListeners = vi.hoisted(() => [] as (() => void)[]);
vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  addChatsUpdatedListener: (listener: () => void) => chatsListeners.push(listener),
  removeChatsUpdatedListener: (listener: () => void) => {
    const index = chatsListeners.indexOf(listener);
    if (index >= 0) chatsListeners.splice(index, 1);
  },
}));

import m from "mithril";
import type { AskDelivery, ReportedFailure, StartOptions, StartOutcome } from "@imbue/workspace-ui/src/app_contract";
import { readFailure } from "@imbue/workspace-ui/src/models/failure";
import { HttpError } from "@imbue/workspace-ui/src/models/http";
import { rowMenuRows } from "./ChatRail";
import { hasPendingDelete } from "./pendingDeletes";
import type { ChatRow } from "./rows";
import { RootNoticeView, raiseRootNotice } from "./rootNotice";
import { chatRailAttrsFixture } from "./chatRailAttrsFixture";

const ROW: ChatRow = {
  chatId: "agent-1",
  kind: "agent",
  title: "Plan the trip",
  status: "idle",
  labels: {},
  agentIds: ["agent-1"],
  lastActiveMs: null,
  isMain: false,
  hasMention: false,
  participants: [],
};

function select(key: string): void {
  const row = rowMenuRows(chatRailAttrsFixture({ rows: [ROW] }), ROW).find(
    (candidate) => candidate.kind === "action" && candidate.key === key,
  ) as { onSelect: () => void };
  row.onSelect();
}

/** A connection to a shell that can open the bug report when ``canReportBug``, recording each time it is asked to
 *  report one or to ask an agent; an ask answers ``askOutcome``. */
function reportingConnection(
  canReportBug: boolean,
  askOutcome: Promise<StartOutcome> = Promise.resolve({ isOpened: true }),
): {
  hasShell: () => boolean;
  askAgent: ReturnType<
    typeof vi.fn<(text: string, options?: StartOptions, delivery?: AskDelivery) => Promise<StartOutcome>>
  >;
  canReportBug: () => boolean;
  reportBug: (failure: ReportedFailure) => void;
} {
  return {
    hasShell: () => true,
    askAgent: vi.fn(() => askOutcome),
    canReportBug: () => canReportBug,
    reportBug: vi.fn(),
  };
}

async function rendered(connection = reportingConnection(true)): Promise<HTMLElement> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  const root = document.createElement("div");
  m.render(root, m(RootNoticeView, { connection }));
  return root;
}

/** The labels of the notice's buttons, in order. */
function labels(root: HTMLElement): string[] {
  return Array.from(root.querySelectorAll("button")).map((b) => b.textContent ?? "");
}

function httpError(status: number, body: unknown): HttpError {
  return new HttpError(readFailure({ status, body, rawText: JSON.stringify(body), requestKind: "change" }));
}

function button(root: HTMLElement, label: string): HTMLElement | undefined {
  return Array.from(root.querySelectorAll("button")).find((b) => b.textContent === label);
}

describe("the chat list's failed verbs", () => {
  beforeEach(() => {
    verbs.stopChat.mockReset();
    verbs.destroyChat.mockReset();
    chatsListeners.length = 0;
    vi.stubGlobal("confirm", () => true);
    vi.stubGlobal("alert", () => {
      throw new Error("no browser alert");
    });
  });

  it("tells a refused Stop chat plainly, with the command output behind See details, and tries it again", async () => {
    verbs.stopChat.mockRejectedValue(
      httpError(500, { detail: "Failed to stop agent 'plan': mngr exited with code 1" }),
    );
    select("stop");
    const connection = reportingConnection(true);
    const root = await rendered(connection);

    expect(root.textContent).toContain(`Couldn't stop "Plan the trip". It's still running. Try again.`);
    expect(root.querySelector(".see-details-text")?.textContent).toContain("mngr exited with code 1");
    button(root, "Try again")!.click();
    expect(verbs.stopChat).toHaveBeenCalledTimes(2);
  });

  it("offers an agent and no Report a bug beside it, asks it as Imbue Studio, and closes once its chat opened", async () => {
    verbs.stopChat.mockRejectedValue(
      httpError(500, { detail: "Failed to stop agent 'plan': mngr exited with code 1" }),
    );
    select("stop");
    let answer: (outcome: StartOutcome) => void = () => undefined;
    const connection = reportingConnection(
      true,
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    const root = await rendered(connection);
    expect(labels(root)).toEqual(["Cancel", "Ask an agent to fix this", "Try again"]);

    button(root, "Ask an agent to fix this")!.click();
    const [text, options] = connection.askAgent.mock.calls[0]!;
    expect(text).toMatch(
      /^Imbue Studio showed the user this failure: Couldn't stop "Plan the trip"\. It's still running\. Try again\./,
    );
    expect(text).toContain("mngr exited with code 1");
    expect(options).toEqual({ showsOutcome: true });
    m.render(root, m(RootNoticeView, { connection }));
    expect(button(root, "Asking an agent…")?.getAttribute("aria-disabled")).toBe("true");
    expect(button(root, "Try again")?.getAttribute("aria-disabled")).toBe("true");

    answer({ isOpened: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    m.render(root, m(RootNoticeView, { connection }));
    expect(root.textContent).not.toContain("Couldn't stop");
  });

  it("tells a refused ask in the notice, then offers a draft of it and one bug report that files both", async () => {
    verbs.stopChat.mockRejectedValue(
      httpError(500, { detail: "Failed to stop agent 'plan': mngr exited with code 1" }),
    );
    select("stop");
    const refused = { message: "Chat couldn't open that.", details: "HTTP 503", isHelpOwed: true };
    const connection = reportingConnection(true, Promise.resolve({ isOpened: false, failure: refused }));
    const root = await rendered(connection);

    button(root, "Ask an agent to fix this")!.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    m.render(root, m(RootNoticeView, { connection }));

    expect(root.textContent).toContain("Chat couldn't open that.");
    expect(labels(root)).toEqual([
      "Cancel",
      "Ask an agent to fix this",
      "Draft it in a chat",
      "Report a bug",
      "Try again",
    ]);
    button(root, "Draft it in a chat")!.click();
    const [asked, drafted] = connection.askAgent.mock.calls;
    expect(drafted).toEqual([asked![0], { showsOutcome: true }, "draft"]);
    button(root, "Report a bug")!.click();
    expect(connection.reportBug).toHaveBeenCalledExactlyOnceWith({
      message: `Couldn't stop "Plan the trip". It's still running. Try again. ` + "Chat couldn't open that.",
      details: expect.stringMatching(/mngr exited with code 1[\s\S]*\n\nHTTP 503$/),
    });
  });

  it("offers no agent on a notice that withholds one, where the ask would go through what failed", async () => {
    raiseRootNotice({
      title: "Couldn't send your message",
      lines: {
        body: ["Your message couldn't be sent to a chat. Try again."],
        details: "HTTP 500",
        isHelpOwed: true,
      },
      retry: null,
      isAgentWithheld: true,
    });
    expect(labels(await rendered())).toEqual(["OK", "Report a bug"]);
    // Only where the shell said Imbue Studio can open a bug report.
    expect(labels(await rendered(reportingConnection(false)))).toEqual(["OK"]);
  });

  it("says a chat that is gone is gone, with nothing to try again", async () => {
    verbs.stopChat.mockRejectedValue(
      httpError(404, {
        detail: "Chat 'agent-1' not found",
        user_facing_message: "This chat is no longer here.",
        kind: "chat_gone",
        routine: true,
      }),
    );
    select("stop");
    const root = await rendered();

    expect(root.textContent).toContain("This chat is no longer here.");
    expect(root.textContent).not.toContain("still running");
    expect(labels(root)).toEqual(["OK"]);
  });

  it("says a delete nothing answered may have run, offers no retry, and keeps the chat off the list until it updates", async () => {
    verbs.destroyChat.mockRejectedValue(httpError(502, null));
    select("delete");
    const root = await rendered();

    expect(root.textContent).toContain(`The workspace didn't answer. "Plan the trip" may not have been deleted.`);
    expect(button(root, "Try again")).toBeUndefined();
    expect(hasPendingDelete()).toBe(true);

    for (const listener of [...chatsListeners]) listener();
    expect(hasPendingDelete()).toBe(false);
  });

  it("shows a half-done delete's own sentence and brings a refused one back", async () => {
    verbs.destroyChat.mockRejectedValue(
      httpError(500, {
        detail: "the agent was destroyed but its record could not be removed",
        user_facing_message:
          "This chat was only partly deleted: its agent is gone, but its conversation could not be removed. If the " +
          "chat comes back later, delete it again.",
      }),
    );
    select("delete");
    const root = await rendered();

    expect(root.textContent).toContain("This chat was only partly deleted");
    expect(button(root, "Ask an agent to fix this")).toBeDefined();
    expect(hasPendingDelete()).toBe(false);
  });
});

describe("a message from outside a chat that the chat would not take", () => {
  it("says the message is in the chat's box when it was refused, with the refusal behind See details", async () => {
    const { describeIntakeApplyFailure } = await import("./rootNotice");
    const refused = httpError(400, { detail: "A pick names one of the chats that can take a message" });
    const lines = describeIntakeApplyFailure(refused, true);
    expect(lines.body).toEqual(["Your message couldn't be sent to this chat. It's in the box below.", null]);
    expect(lines.details).toContain("A pick names one of the chats");
    expect(lines.isHelpOwed).toBe(false);
  });

  it("owes help for a refusal the chat app wrote a sentence about, unless it called the refusal routine", async () => {
    const { describeIntakeApplyFailure } = await import("./rootNotice");
    const failed = httpError(500, {
      detail: "mngr failed",
      user_facing_message: "Chat couldn't take this. Try again.",
    });
    expect(describeIntakeApplyFailure(failed, true).isHelpOwed).toBe(true);
    const gone = httpError(404, {
      detail: "Chat 'agent-1' not found",
      user_facing_message: "That chat is no longer here. Pick another chat, or start a new one.",
      kind: "chat_gone",
      routine: true,
    });
    expect(describeIntakeApplyFailure(gone, false).isHelpOwed).toBe(false);
  });

  it("does not claim where the message went when nothing answered", async () => {
    const { describeIntakeApplyFailure } = await import("./rootNotice");
    const unanswered = httpError(504, null);
    expect(describeIntakeApplyFailure(unanswered, false).body).toEqual([
      "The workspace didn't answer. Your message may not have reached this chat.",
    ]);
  });
});
