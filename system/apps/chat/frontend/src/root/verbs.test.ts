// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  postJson: vi.fn(async (_url: string, _body: unknown): Promise<unknown> => ({})),
  dropCachedTranscript: vi.fn((_chatId: string) => undefined),
}));
vi.mock("@imbue/workspace-ui/src/models/http", () => ({ postJson: mocks.postJson }));
vi.mock("../models/transcriptCache", () => ({ dropCachedTranscript: mocks.dropCachedTranscript }));

import { destroyChat } from "./verbs";

describe("destroyChat", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("forgets the chat's cached transcript once the server has removed it", async () => {
    await destroyChat("chat-a");

    expect(mocks.postJson).toHaveBeenCalledWith(expect.stringContaining("/api/chats/chat-a/destroy"), {});
    expect(mocks.dropCachedTranscript).toHaveBeenCalledWith("chat-a");
  });

  it("keeps the cached transcript when the server refuses, since the chat is still there", async () => {
    mocks.postJson.mockRejectedValueOnce(new Error("refused"));

    await expect(destroyChat("chat-a")).rejects.toThrow("refused");

    expect(mocks.dropCachedTranscript).not.toHaveBeenCalled();
  });
});
