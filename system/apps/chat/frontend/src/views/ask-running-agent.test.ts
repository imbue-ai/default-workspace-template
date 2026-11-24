import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  chats: [] as unknown[],
  mainChatId: null as string | null,
  sendMessage: vi.fn(async (_chatId: string, _text: string, _messageId: string): Promise<string | null> => "ok"),
  openChatPage: vi.fn(),
  shellConnection: null as unknown,
}));

vi.mock("../models/Chats", () => ({
  getChats: () => mocks.chats,
  getMainChatId: () => mocks.mainChatId,
  getChatById: (chatId: string) => (mocks.chats as ChatSnapshot[]).find((chat) => chat.chat_id === chatId),
}));
vi.mock("../models/Response", () => ({
  sendMessage: mocks.sendMessage,
  mintMessageId: () => "minted-id",
}));
vi.mock("../shell", () => ({ openChatPage: mocks.openChatPage }));
vi.mock("../shellConnection", () => ({ getShellConnection: () => mocks.shellConnection }));

import { askAgentToFix } from "@imbue/workspace-ui/src/app_contract";
import type { ChatSnapshot } from "../models/Chats";
import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import { askConnectionToChat, chatToAskAbout } from "./ask-running-agent";
import { withRefusedAsk } from "./failure-lines";

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
    mocks.chats = [chatSnapshotFixture("chat-main", { title: "Main chat" })];
    mocks.sendMessage.mockReset();
    mocks.sendMessage.mockResolvedValue("ok");
    mocks.openChatPage.mockClear();
    mocks.shellConnection = null;
  });

  it("sends the task to that chat as a message that runs the assist flow, written as Imbue Studio, then shows the chat", async () => {
    const outcome = await askAgentToFix(askConnectionToChat("chat-main"), FAILURE);

    expect(outcome).toEqual({ isOpened: true });
    expect(mocks.sendMessage).toHaveBeenCalledExactlyOnceWith(
      "chat-main",
      '/assist Imbue Studio showed the user this failure: The chat "Chat 3" couldn\'t be started.\n\n' +
        "Details:\n```\nmngr create exited with code 1\n```\n\n" +
        "Find out why it failed and fix it, then tell the user what was wrong and what you changed.",
      "minted-id",
    );
    expect(mocks.openChatPage).toHaveBeenCalledExactlyOnceWith("chat-main");
  });

  it("leads a refused send's sentence with the chat that was asked, and leaves the user where they were", async () => {
    mocks.sendMessage.mockRejectedValueOnce(
      refusal(500, {
        detail: "A dialog (a permission prompt) is open in the agent's terminal. Clear it there.",
        user_facing_message: "The agent is waiting on something in its terminal. Answer it there and try again.",
        kind: "input_blocked",
        expected: true,
      }),
    );

    const outcome = await askAgentToFix(askConnectionToChat("chat-main"), FAILURE);

    expect(outcome).toEqual({
      isOpened: false,
      failure: {
        message:
          'Couldn\'t ask "Main chat". The agent is waiting on something in its terminal. Answer it there and try again.',
        details:
          'Asking "Main chat":\nHTTP 500\n' +
          "A dialog (a permission prompt) is open in the agent's terminal. Clear it there.",
        isHelpOwed: false,
      },
    });
    expect(mocks.openChatPage).not.toHaveBeenCalled();
  });

  it("names the chat that was asked for a refusal nothing worded, which owes help", async () => {
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "mngr message exited 1" }));

    const outcome = await askAgentToFix(askConnectionToChat("chat-main"), FAILURE);

    expect(outcome).toMatchObject({
      isOpened: false,
      failure: {
        message: 'Couldn\'t ask "Main chat". Try again.',
        details: expect.stringContaining("mngr message exited 1"),
        isHelpOwed: true,
      },
    });
  });

  it("says a send nothing answered may have reached the chat that was asked", async () => {
    mocks.sendMessage.mockRejectedValueOnce(Object.assign(new Error("null"), { code: 0, response: null }));

    const outcome = await askAgentToFix(askConnectionToChat("chat-main"), FAILURE);

    expect(outcome).toMatchObject({
      isOpened: false,
      failure: {
        message: "The workspace didn't answer, so Imbue Studio can't tell whether \"Main chat\" was asked.",
        isHelpOwed: false,
      },
    });
  });

  it("still says it is about another chat once the list no longer names the chat that was asked", async () => {
    mocks.chats = [];
    mocks.sendMessage.mockRejectedValueOnce(refusal(404, { detail: "no such chat" }));

    const outcome = await askAgentToFix(askConnectionToChat("chat-main"), FAILURE);

    expect(outcome).toMatchObject({ isOpened: false, failure: { message: "Couldn't ask the other chat." } });
  });

  it("leaves a refused ask's draft to the shell, which writes the assist command itself", async () => {
    const askAgent = vi.fn(async () => ({ isOpened: true }));
    mocks.shellConnection = { askAgent };

    const outcome = await askAgentToFix(askConnectionToChat("chat-main"), FAILURE, { showsOutcome: true }, "draft");

    expect(outcome).toEqual({ isOpened: true });
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(askAgent).toHaveBeenCalledExactlyOnceWith(
      expect.stringMatching(/^Imbue Studio showed the user this failure: The chat "Chat 3" couldn't be started\./),
      { showsOutcome: true },
      "draft",
    );
  });

  it("drafts nothing, and sends nothing, where no shell frames the page", async () => {
    const outcome = await askAgentToFix(askConnectionToChat("chat-main"), FAILURE, { showsOutcome: true }, "draft");

    expect(outcome.isOpened).toBe(false);
    expect(mocks.sendMessage).not.toHaveBeenCalled();
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

describe("a failure told together with the refused ask about it", () => {
  const ASK_SENTENCE = 'Couldn\'t ask "Main chat". Try again.';

  it.each([
    [
      "both have details",
      "HTTP 500\nmngr start exited 1",
      'Asking "Main chat":\nHTTP 502',
      'HTTP 500\nmngr start exited 1\n\nAsking "Main chat":\nHTTP 502',
    ],
    ["only the ask has details", null, 'Asking "Main chat":\nHTTP 502', 'Asking "Main chat":\nHTTP 502'],
    ["only the failure has details", "HTTP 500\nmngr start exited 1", null, "HTTP 500\nmngr start exited 1"],
    ["neither has details", null, null, null],
  ])("keeps one set of details when %s", (_case, failureDetails, askDetails, expected) => {
    const told = withRefusedAsk(
      { body: ["The agent isn't running and didn't start."], details: failureDetails, isHelpOwed: false },
      { message: ASK_SENTENCE, details: askDetails, isHelpOwed: true },
    );

    expect(told.details).toBe(expected);
  });

  it("says the ask's sentence last and owes help when either failure does", () => {
    const told = withRefusedAsk(
      {
        body: ["The agent isn't running and didn't start.", "Restart it to send your message."],
        details: null,
        isHelpOwed: false,
      },
      { message: ASK_SENTENCE, details: null, isHelpOwed: true },
    );

    expect(told.body).toEqual([
      "The agent isn't running and didn't start.",
      "Restart it to send your message.",
      ASK_SENTENCE,
    ]);
    expect(told.isHelpOwed).toBe(true);
  });
});
