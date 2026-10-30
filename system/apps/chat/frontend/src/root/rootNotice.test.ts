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
import { readFailure } from "@imbue/workspace-ui/src/models/failure";
import { HttpError } from "@imbue/workspace-ui/src/models/http";
import { isDeleting, rowMenuRows } from "./ChatRail";
import type { ChatRow } from "./rows";
import { RootNoticeView } from "./rootNotice";
import { chatRailAttrsFixture } from "./chatRailAttrsFixture";

const ROW: ChatRow = {
  chatId: "agent-1",
  title: "Plan the trip",
  status: "idle",
  labels: {},
  agentIds: ["agent-1"],
  lastActiveMs: null,
  isProvisional: false,
  isMain: false,
};

function select(key: string): void {
  const row = rowMenuRows(chatRailAttrsFixture({ rows: [ROW] }), ROW).find(
    (candidate) => candidate.kind === "action" && candidate.key === key,
  ) as { onSelect: () => void };
  row.onSelect();
}

/** A connection whose shell can open the bug report, recording each time it is asked to. */
function reportingConnection(canReportBug: boolean): { canReportBug: () => boolean; reportBug: () => void } {
  return { canReportBug: () => canReportBug, reportBug: vi.fn() };
}

async function rendered(connection = reportingConnection(true)): Promise<HTMLElement> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  const root = document.createElement("div");
  m.render(root, m(RootNoticeView, { connection }));
  return root;
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

    expect(root.textContent).toContain(
      `Couldn't stop "Plan the trip". It's still running. Try again. If this persists, report a bug.`,
    );
    expect(root.querySelector(".see-details-text")?.textContent).toContain("mngr exited with code 1");
    button(root, "Report a bug")!.click();
    expect(connection.reportBug).toHaveBeenCalledTimes(1);
    button(root, "Try again")!.click();
    expect(verbs.stopChat).toHaveBeenCalledTimes(2);
  });

  it("offers Report a bug only where the shell said Imbue Studio can open one", async () => {
    verbs.stopChat.mockRejectedValue(
      httpError(500, { detail: "Failed to stop agent 'plan': mngr exited with code 1" }),
    );
    select("stop");
    const root = await rendered(reportingConnection(false));

    expect(root.textContent).toContain("If this persists, report a bug.");
    expect(button(root, "Report a bug")).toBeUndefined();
  });

  it("says a chat that is gone is gone, with nothing to try again", async () => {
    verbs.stopChat.mockRejectedValue(
      httpError(404, {
        detail: "Chat 'agent-1' not found",
        user_facing_message: "This chat is no longer here.",
        kind: "chat_gone",
      }),
    );
    select("stop");
    const root = await rendered();

    expect(root.textContent).toContain("This chat is no longer here.");
    expect(root.textContent).not.toContain("still running");
    expect(button(root, "Try again")).toBeUndefined();
  });

  it("says a delete nothing answered may have run, offers no retry, and keeps the row marked until the list updates", async () => {
    verbs.destroyChat.mockRejectedValue(httpError(502, null));
    select("delete");
    const root = await rendered();

    expect(root.textContent).toContain(
      `The workspace didn't answer, so Imbue Studio can't tell whether "Plan the trip" was deleted.`,
    );
    expect(button(root, "Try again")).toBeUndefined();
    expect(isDeleting("agent-1")).toBe(true);

    for (const listener of [...chatsListeners]) listener();
    expect(isDeleting("agent-1")).toBe(false);
  });

  it("shows a half-done delete's own sentence and unmarks a refused one", async () => {
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
    expect(isDeleting("agent-1")).toBe(false);
  });
});

describe("a message from outside a chat that the chat would not take", () => {
  it("says the message is in the chat's box when it was refused, with the refusal behind See details", async () => {
    const { describeIntakeApplyFailure } = await import("./rootNotice");
    const refused = httpError(400, { detail: "A pick names one of the chats that can take a message" });
    const lines = describeIntakeApplyFailure(refused, true);
    expect(lines.body).toEqual(["Your message couldn't be sent to this chat. It's in the box below.", null]);
    expect(lines.details).toContain("A pick names one of the chats");
  });

  it("does not claim where the message went when nothing answered", async () => {
    const { describeIntakeApplyFailure } = await import("./rootNotice");
    const unanswered = httpError(504, null);
    expect(describeIntakeApplyFailure(unanswered, false).body).toEqual([
      "The workspace didn't answer, so Imbue Studio can't tell whether your message reached this chat.",
    ]);
  });
});
