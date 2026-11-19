// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  chat: null as unknown,
  sends: [] as [string, string][],
  sendRejects: false,
}));
vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: () => state.chat ?? undefined,
}));
vi.mock("../models/Response", () => ({
  mintMessageId: () => "message-1",
  sendMessage: (chatId: string, message: string) => {
    state.sends.push([chatId, message]);
    return state.sendRejects ? Promise.reject(new Error("The chat app could not be reached.")) : Promise.resolve("ok");
  },
}));
vi.mock("@imbue/workspace-ui/src/models/request-error", () => ({
  describeRequestError: (error: unknown) => (error as Error).message,
}));

import m from "mithril";
import { chatSnapshotFixture, handoffStateFixture } from "../models/chatSnapshotFixture";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import { INTERRUPTED_TITLE, InterruptedNotice, RESUME_MESSAGE } from "./InterruptedNotice";

const ROOT = () => document.getElementById("root") as HTMLElement;

function render(chatId = "agent-1"): void {
  m.render(ROOT(), m(InterruptedNotice as never, { chatId }));
}

async function flush(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

describe("the interrupted-chat notice", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>';
    state.sends = [];
    state.sendRejects = false;
    state.chat = chatSnapshotFixture("agent-1", { status: "interrupted", active_agent: { is_interrupted: true } });
  });

  it("shows one fixed line and a Resume, and nothing for a chat that was not cut off", () => {
    render();
    expect(ROOT().querySelector(".interrupted-notice-title")?.textContent).toBe(INTERRUPTED_TITLE);
    expect(ROOT().querySelector(".interrupted-resume-button")?.textContent).toBe("Resume");

    state.chat = chatSnapshotFixture("agent-1");
    render();
    expect(ROOT().querySelector(".interrupted-notice")).toBeNull();
  });

  it("leaves a switching chat to the switch's own notices", () => {
    state.chat = chatSnapshotFixture("agent-1", {
      handoff: handoffStateFixture(),
      active_agent: { is_interrupted: true },
    });
    render();
    expect(ROOT().querySelector(".interrupted-notice")).toBeNull();
  });

  it("sends the agent a message to carry on only when Resume is pressed, shown at once", async () => {
    render();
    await flush();
    expect(state.sends).toEqual([]);

    (ROOT().querySelector(".interrupted-resume-button") as HTMLElement).click();
    expect(getOutgoingMessages("agent-1").map((sent) => sent.content)).toEqual([RESUME_MESSAGE]);
    await flush();

    expect(state.sends).toEqual([["agent-1", RESUME_MESSAGE]]);
  });

  it("says why a Resume did not go, and takes its message back", async () => {
    state.sendRejects = true;
    state.chat = chatSnapshotFixture("agent-2", { status: "interrupted", active_agent: { is_interrupted: true } });
    render("agent-2");
    (ROOT().querySelector(".interrupted-resume-button") as HTMLElement).click();
    await flush();
    render("agent-2");

    expect(ROOT().querySelector(".interrupted-resume-error")?.textContent).toBe("The chat app could not be reached.");
    expect(getOutgoingMessages("agent-2")).toEqual([]);
  });
});
