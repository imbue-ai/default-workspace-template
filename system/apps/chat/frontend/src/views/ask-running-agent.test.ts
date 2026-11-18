import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  chats: [] as unknown[],
  mainChatId: null as string | null,
  sendMessage: vi.fn(async (_chatId: string, _text: string, _messageId: string): Promise<string | null> => "ok"),
  openChatPage: vi.fn(),
  shellConnection: null as unknown,
}));

vi.mock("../models/Chats", () => ({ getChats: () => mocks.chats, getMainChatId: () => mocks.mainChatId }));
vi.mock("../models/Response", () => ({
  sendMessage: mocks.sendMessage,
  mintMessageId: () => "minted-id",
}));
vi.mock("../shell", () => ({ openChatPage: mocks.openChatPage }));
vi.mock("../shellConnection", () => ({ getShellConnection: () => mocks.shellConnection }));

import { askAgentToFix } from "@imbue/workspace-ui/src/app_contract";
import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import { askConnectionToChat, chatToAskAbout } from "./ask-running-agent";

/** A refusal as mithril rejects with it: the parsed body, its status as ``code``, and the raw body as the message. */
function refusal(status: number, body: Record<string, unknown>): Error {
  return Object.assign(new Error(JSON.stringify(body)), { code: status, response: body });
}

describe("the chat an ask about a chat that cannot start goes to", () => {
  beforeEach(() => {
    mocks.chats = [];
    mocks.mainChatId = null;
  });

  it("is the main chat, whichever chat was messaged last", () => {
    mocks.chats = [
      chatSnapshotFixture("chat-main", { last_messaged_at: 100 }),
      chatSnapshotFixture("chat-busy", { last_messaged_at: 900 }),
    ];
    mocks.mainChatId = "chat-main";

    expect(chatToAskAbout("chat-failing")).toBe("chat-main");
  });

  it("is the most recently messaged other agent chat when the main chat is the one failing", () => {
    mocks.chats = [
      chatSnapshotFixture("chat-main", { last_messaged_at: 900 }),
      chatSnapshotFixture("chat-older", { last_messaged_at: 100 }),
      chatSnapshotFixture("chat-newer", { last_messaged_at: 500 }),
      chatSnapshotFixture("chat-unmessaged"),
    ];
    mocks.mainChatId = "chat-main";

    expect(chatToAskAbout("chat-main")).toBe("chat-newer");
  });

  it("passes over a main chat that is a room, which has no agent to do the work, and every other room", () => {
    mocks.chats = [
      chatSnapshotFixture("chat-main", { kind: "room", last_messaged_at: 900 }),
      chatSnapshotFixture("chat-lounge", { kind: "room", last_messaged_at: 800 }),
      chatSnapshotFixture("chat-agent", { last_messaged_at: 100 }),
    ];
    mocks.mainChatId = "chat-main";

    expect(chatToAskAbout("chat-failing")).toBe("chat-agent");
  });

  it("takes an agent chat nobody has messaged when it is the only one", () => {
    mocks.chats = [chatSnapshotFixture("chat-failing", { last_messaged_at: 900 }), chatSnapshotFixture("chat-quiet")];

    expect(chatToAskAbout("chat-failing")).toBe("chat-quiet");
  });

  it("is no chat when the failing chat is the only agent chat", () => {
    mocks.chats = [
      chatSnapshotFixture("chat-failing", { last_messaged_at: 900 }),
      chatSnapshotFixture("chat-lounge", { kind: "room", last_messaged_at: 800 }),
    ];
    mocks.mainChatId = "chat-failing";

    expect(chatToAskAbout("chat-failing")).toBeNull();
  });
});

describe("an ask sent to a running chat", () => {
  const FAILURE = { whatFailed: 'The chat "Chat 3" couldn\'t be started', details: "mngr create exited with code 1" };

  beforeEach(() => {
    mocks.sendMessage.mockReset();
    mocks.sendMessage.mockResolvedValue("ok");
    mocks.openChatPage.mockClear();
    mocks.shellConnection = null;
  });

  it("sends the task to that chat as a message, then shows the chat", async () => {
    const outcome = await askAgentToFix(askConnectionToChat("chat-main"), FAILURE);

    expect(outcome).toEqual({ isOpened: true });
    expect(mocks.sendMessage).toHaveBeenCalledExactlyOnceWith(
      "chat-main",
      'The chat "Chat 3" couldn\'t be started.\n\n' +
        "Details:\n```\nmngr create exited with code 1\n```\n\n" +
        "Find out why this failed and fix it. Then tell me what was wrong and what you changed.",
      "minted-id",
    );
    expect(mocks.openChatPage).toHaveBeenCalledExactlyOnceWith("chat-main");
  });

  it("answers a refused send with its sentence and details, and leaves the user where they were", async () => {
    mocks.sendMessage.mockRejectedValueOnce(
      refusal(500, {
        detail: "Could not start agent 'Chat-1' for a send: mngr start exited 1",
        user_facing_message: "This chat's agent couldn't be started. Try again. If this persists, report a bug.",
        reportable: true,
      }),
    );

    const outcome = await askAgentToFix(askConnectionToChat("chat-main"), FAILURE);

    expect(outcome).toEqual({
      isOpened: false,
      failure: {
        message: "This chat's agent couldn't be started. Try again. If this persists, report a bug.",
        details: expect.stringContaining("Could not start agent 'Chat-1' for a send: mngr start exited 1"),
        isReportable: true,
      },
    });
    expect(mocks.openChatPage).not.toHaveBeenCalled();
  });

  it("says a send nothing answered may have reached the agent", async () => {
    mocks.sendMessage.mockRejectedValueOnce(Object.assign(new Error("null"), { code: 0, response: null }));

    const outcome = await askAgentToFix(askConnectionToChat("chat-main"), FAILURE);

    expect(outcome).toMatchObject({
      isOpened: false,
      failure: {
        message: "The workspace didn't answer, so Imbue Studio can't tell whether the agent was asked.",
        isReportable: false,
      },
    });
  });

  it("reports a refused ask through the shell that frames the page", () => {
    const reportBug = vi.fn();
    mocks.shellConnection = { canReportBug: () => true, reportBug };
    const connection = askConnectionToChat("chat-main");

    connection.reportBug({ message: "The agent couldn't be asked.", details: null });

    expect(connection.canReportBug()).toBe(true);
    expect(reportBug).toHaveBeenCalledExactlyOnceWith({ message: "The agent couldn't be asked.", details: null });
  });
});
