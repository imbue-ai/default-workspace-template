// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
  return {
    chat: null as unknown,
    accounts: [] as unknown[],
    retries: [] as [string, string][],
    retryRejects: false,
    started: [] as string[],
  };
});
vi.mock("../shell", () => ({
  startChatOnAccount: (accountId: string) => {
    state.started.push(accountId);
    return Promise.resolve(true);
  },
}));
vi.mock("../models/Chats", () => ({ getChatById: () => state.chat ?? undefined }));
vi.mock("../models/HarnessCatalog", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/HarnessCatalog")>()),
  getHarnessCatalog: (await import("../models/harnessCatalogFixture")).harnessCatalogFixture,
}));
vi.mock("../models/Providers", () => ({ getAccounts: () => state.accounts }));
vi.mock("../models/Handoffs", () => ({
  retryHandoff: (chatId: string, accountId: string) => {
    state.retries.push([chatId, accountId]);
    return state.retryRejects ? Promise.reject(new Error("no such account")) : Promise.resolve({ phase: "switching" });
  },
}));
vi.mock("@imbue/workspace-ui/src/models/request-error", () => ({
  describeRequestError: (error: unknown) => (error as Error).message,
}));

import m from "mithril";
import { chatSnapshotFixture, handoffStateFixture, rebindStateFixture } from "../models/chatSnapshotFixture";
import { HandoffFailedNotice } from "./HandoffFailedNotice";

const ROOT = () => document.getElementById("root") as HTMLElement;
const ACCOUNTS = [
  { id: "acct-anthropic", label: "Anthropic (Claude Code)", harness: "claude", lane: "anthropic" },
  { id: "acct-anthropic-2", label: "Anthropic 2 (Claude Code)", harness: "claude", lane: "anthropic" },
  { id: "acct-openai", label: "OpenAI (Codex)", harness: "codex", lane: "openai" },
  { id: "acct-google", label: "Google (Antigravity CLI)", harness: "antigravity", lane: "google" },
];

function render(): void {
  m.render(ROOT(), m(HandoffFailedNotice as never, { chatId: "agent-1" }));
}

async function flush(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

describe("the failed-switch notice", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>';
    state.accounts = ACCOUNTS;
    state.retries.length = 0;
    state.started.length = 0;
    state.retryRejects = false;
    state.chat = chatSnapshotFixture("agent-1", {
      handoff: handoffStateFixture({ phase: "failed", error: "mngr create exited with code 3\nno such template" }),
    });
  });

  it("renders nothing unless the chat's switch failed", () => {
    state.chat = chatSnapshotFixture("agent-1");
    render();
    expect(ROOT().textContent).toBe("");
    state.chat = chatSnapshotFixture("agent-1", { handoff: handoffStateFixture({ phase: "switching" }) });
    render();
    expect(ROOT().textContent).toBe("");
  });

  it("shows the reason and retries on the failed target by default", async () => {
    render();
    expect(ROOT().querySelector(".handoff-failed-title")?.textContent).toBe("Could not start Codex");
    expect(ROOT().querySelector(".handoff-failed-reason")?.textContent).toContain("no such template");
    const select = ROOT().querySelector<HTMLSelectElement>("select.handoff-retry-account");
    expect(select?.value).toBe("acct-openai");
    expect([...(select?.options ?? [])].map((option) => option.textContent)).toEqual(ACCOUNTS.map((a) => a.label));

    ROOT().querySelector<HTMLButtonElement>(".handoff-retry-button")?.click();
    await flush();
    expect(state.retries).toEqual([["agent-1", "acct-openai"]]);
  });

  it("names the model pick when that is the step that failed", () => {
    state.chat = chatSnapshotFixture("agent-1", {
      handoff: handoffStateFixture({ phase: "failed", error: "Unknown model 'gpt-6-astra'", failed_step: "model" }),
    });
    render();
    expect(ROOT().querySelector(".handoff-failed-title")?.textContent).toBe("Could not set the model on Codex");
    expect(ROOT().querySelector(".handoff-failed-reason")?.textContent).toContain("Unknown model 'gpt-6-astra'");
    expect(ROOT().querySelector(".handoff-retry-button")).not.toBeNull();
  });

  it("retries on the account the user picks, and shows a refusal without losing the notice", async () => {
    state.retryRejects = true;
    render();
    const select = ROOT().querySelector<HTMLSelectElement>("select.handoff-retry-account");
    if (select === null) throw new Error("no account picker");
    select.value = "acct-google";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    ROOT().querySelector<HTMLButtonElement>(".handoff-retry-button")?.click();
    await flush();
    render();
    expect(state.retries).toEqual([["agent-1", "acct-google"]]);
    expect(ROOT().querySelector(".handoff-retry-error")?.textContent).toBe("no such account");
    expect(ROOT().querySelector(".handoff-failed-title")).not.toBeNull();
  });

  it("offers a new chat on the picked account instead of a retry", () => {
    render();
    const select = ROOT().querySelector<HTMLSelectElement>("select.handoff-retry-account");
    if (select === null) throw new Error("no account picker");
    select.value = "acct-google";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    ROOT().querySelector<HTMLButtonElement>(".handoff-new-chat-button")?.click();
    expect(state.started).toEqual(["acct-google"]);
    expect(state.retries).toEqual([]);
  });

  it("names the model pick for a rebind whose agent came back but could not take it", () => {
    state.chat = chatSnapshotFixture("agent-1", {
      active_agent: { harness: "claude", account_id: "acct-anthropic-2" },
      handoff: rebindStateFixture({ phase: "failed", error: "Unknown model 'gpt-6-astra'", failed_step: "model" }),
    });
    render();
    expect(ROOT().querySelector(".handoff-failed-title")?.textContent).toBe("Could not set the model on Claude Code");
    expect(ROOT().querySelector(".handoff-failed-reason")?.textContent).toContain("Unknown model 'gpt-6-astra'");
  });

  it("names the restart and offers only the agent's own harness and lane for a failed rebind", () => {
    state.chat = chatSnapshotFixture("agent-1", {
      active_agent: { harness: "claude", account_id: "acct-anthropic-2" },
      handoff: rebindStateFixture({ phase: "failed", error: "mngr start exited with code 1" }),
    });
    render();
    expect(ROOT().querySelector(".handoff-failed-title")?.textContent).toBe("Could not restart Claude Code");
    const select = ROOT().querySelector<HTMLSelectElement>("select.handoff-retry-account");
    expect([...(select?.options ?? [])].map((option) => option.value)).toEqual(["acct-anthropic", "acct-anthropic-2"]);
    expect(select?.value).toBe("acct-anthropic-2");
  });
});
