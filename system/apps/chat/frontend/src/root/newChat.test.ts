import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  class CreateChatRefusedError extends Error {
    readonly kind: string | null;

    constructor(detail: string, kind: string | null) {
      super(detail);
      this.kind = kind;
    }
  }
  return {
    CreateChatRefusedError,
    // What the fresh read of the account list leaves selected; null when nothing is signed in.
    selectedAccount: null as { id: string } | null,
    createChat: vi.fn(async (_projectId: string, _accountId?: string) => ({
      chatId: "chat-new",
      name: "Chat-1",
      displayName: "Chat 1",
    })),
    awaitingChatForEmptyList: vi.fn(async () => "chat-awaiting"),
    whenAccountsReadyToChoose: vi.fn(async () => undefined),
  };
});

vi.mock("../models/Chats", () => ({
  CreateChatRefusedError: mocks.CreateChatRefusedError,
  createChat: mocks.createChat,
  awaitingChatForEmptyList: mocks.awaitingChatForEmptyList,
}));
vi.mock("../models/Providers", () => ({
  getSelectedAccount: () => mocks.selectedAccount,
  whenAccountsReadyToChoose: mocks.whenAccountsReadyToChoose,
}));

import { startNewChat } from "./newChat";

describe("startNewChat", () => {
  beforeEach(() => {
    mocks.selectedAccount = { id: "acct-live" };
    mocks.createChat.mockClear();
    mocks.awaitingChatForEmptyList.mockClear();
    mocks.whenAccountsReadyToChoose.mockClear();
  });

  it("creates a chat on the account the chat app picks, naming none from the page's list", async () => {
    expect(await startNewChat()).toEqual({ kind: "created", chatId: "chat-new" });
    expect(mocks.whenAccountsReadyToChoose).toHaveBeenCalledOnce();
    expect(mocks.createChat).toHaveBeenCalledOnce();
    const [, accountId] = mocks.createChat.mock.calls[0];
    expect(accountId ?? "").toBe("");
    expect(mocks.awaitingChatForEmptyList).not.toHaveBeenCalled();
  });

  it("opens the chat awaiting a provider without creating anything when nothing is signed in", async () => {
    mocks.selectedAccount = null;

    expect(await startNewChat()).toEqual({ kind: "awaiting_provider", chatId: "chat-awaiting" });
    expect(mocks.createChat).not.toHaveBeenCalled();
  });

  it("opens the chat awaiting a provider when the chat app finds nothing signed in after all", async () => {
    mocks.createChat.mockRejectedValueOnce(
      new mocks.CreateChatRefusedError("Connect an AI provider to start chatting.", "no_provider_account"),
    );

    expect(await startNewChat()).toEqual({ kind: "awaiting_provider", chatId: "chat-awaiting" });
  });

  it("gives the chat app's reason for any other refusal instead of opening a chat", async () => {
    mocks.createChat.mockRejectedValueOnce(
      new mocks.CreateChatRefusedError("Cannot determine work directory for primary agent agent-1", null),
    );

    expect(await startNewChat()).toEqual({
      kind: "refused",
      detail: "Cannot determine work directory for primary agent agent-1",
    });
    expect(mocks.awaitingChatForEmptyList).not.toHaveBeenCalled();
  });

  it("gives the reason when even the chat awaiting a provider cannot be opened", async () => {
    mocks.selectedAccount = null;
    mocks.awaitingChatForEmptyList.mockRejectedValueOnce(new Error("The chat list could not open a new chat (503)"));

    expect(await startNewChat()).toEqual({ kind: "refused", detail: "The chat list could not open a new chat (503)" });
  });
});
