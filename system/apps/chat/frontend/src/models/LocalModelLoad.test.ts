import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  optionId: "local-model/qwen3:8b" as string | null,
}));
vi.mock("mithril", () => ({ default: { request: mocks.request } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));
vi.mock("./Chats", () => ({
  getChatById: () => ({ active_agent: { model_choice: null } }),
}));
vi.mock("./ModelSettings", () => ({
  effectiveChoice: () => (mocks.optionId === null ? null : { matched: { id: mocks.optionId } }),
}));

import { forgetModelLoad, isModelLoadPending, noteSendToModel } from "./LocalModelLoad";

const CHAT_ID = "chat-1";
const EARLIER_TURN = { id: "turn-1", hasAgentWork: true };

describe("a reply waiting on its model being loaded", () => {
  beforeEach(() => {
    mocks.request.mockReset();
    mocks.optionId = "local-model/qwen3:8b";
    forgetModelLoad(CHAT_ID);
  });

  it("asks the backend about the chat's own model as a message is sent", async () => {
    mocks.request.mockResolvedValue({ is_load_pending: true });

    await noteSendToModel(CHAT_ID);

    expect(mocks.request).toHaveBeenCalledWith({
      method: "GET",
      url: "/api/chats/:chatId/model-load",
      params: { chatId: CHAT_ID, model: "local-model/qwen3:8b" },
    });
    expect(isModelLoadPending(CHAT_ID, EARLIER_TURN)).toBe(true);
  });

  it("asks nothing for a chat on a provider's model", async () => {
    mocks.optionId = "claude-opus";

    await noteSendToModel(CHAT_ID);

    expect(mocks.request).not.toHaveBeenCalled();
    expect(isModelLoadPending(CHAT_ID, EARLIER_TURN)).toBe(false);
  });

  it("is not pending when the model is already in memory, or when the backend cannot say", async () => {
    mocks.request.mockResolvedValueOnce({ is_load_pending: false });
    await noteSendToModel(CHAT_ID);
    expect(isModelLoadPending(CHAT_ID, EARLIER_TURN)).toBe(false);

    mocks.request.mockRejectedValueOnce(new Error("offline"));
    await noteSendToModel(CHAT_ID);
    expect(isModelLoadPending(CHAT_ID, EARLIER_TURN)).toBe(false);
  });

  it("lasts through the send and the new turn's silence, and ends with the agent's first work in it", async () => {
    mocks.request.mockResolvedValue({ is_load_pending: true });
    await noteSendToModel(CHAT_ID);

    // The transcript still ends on the turn before the send, then on the new turn with nothing in it.
    expect(isModelLoadPending(CHAT_ID, EARLIER_TURN)).toBe(true);
    expect(isModelLoadPending(CHAT_ID, { id: "turn-2", hasAgentWork: false })).toBe(true);
    expect(isModelLoadPending(CHAT_ID, { id: "turn-2", hasAgentWork: true })).toBe(false);
    // Over for good: a later quiet turn is an ordinary one.
    expect(isModelLoadPending(CHAT_ID, { id: "turn-3", hasAgentWork: false })).toBe(false);
  });

  it("counts the first turn of a chat with no earlier one", async () => {
    mocks.request.mockResolvedValue({ is_load_pending: true });
    await noteSendToModel(CHAT_ID);

    expect(isModelLoadPending(CHAT_ID, null)).toBe(true);
    expect(isModelLoadPending(CHAT_ID, { id: "turn-1", hasAgentWork: false })).toBe(true);
    expect(isModelLoadPending(CHAT_ID, { id: "turn-1", hasAgentWork: true })).toBe(false);
  });

  it("is dropped when the send did not go through", async () => {
    mocks.request.mockResolvedValue({ is_load_pending: true });
    await noteSendToModel(CHAT_ID);

    forgetModelLoad(CHAT_ID);

    expect(isModelLoadPending(CHAT_ID, EARLIER_TURN)).toBe(false);
  });
});
