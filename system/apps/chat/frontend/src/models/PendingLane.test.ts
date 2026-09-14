import { beforeEach, describe, expect, it, vi } from "vitest";

// The pending lane reads the chat and the accounts through their models; both are served from
// mutable holders here, and the chats listener the settlement installs is captured to be driven.
const state = vi.hoisted(() => ({
  chat: null as unknown,
  accounts: [] as { id: string; harness: string }[],
  listeners: [] as ((chats: unknown[]) => void)[],
}));
vi.mock("./Chats", () => ({
  getChatById: () => state.chat ?? undefined,
  addChatsUpdatedListener: (listener: (chats: unknown[]) => void) => state.listeners.push(listener),
}));
vi.mock("./Providers", () => ({
  accountForAgent: (id?: string) => state.accounts.find((account) => account.id === id) ?? null,
}));

import { chatSnapshotFixture } from "./chatSnapshotFixture";
import {
  getPendingAccountId,
  pendingSwitchTarget,
  setPendingAccount,
  trackPendingLaneSettlement,
} from "./PendingLane";

const CODEX_ACCOUNT = { id: "acct-openai", harness: "codex" };
const OTHER_CLAUDE_ACCOUNT = { id: "acct-anthropic-2", harness: "claude" };

describe("the pending lane", () => {
  beforeEach(() => {
    state.chat = chatSnapshotFixture("agent-1", { active_agent: { harness: "claude", account_id: "acct-anthropic" } });
    state.accounts = [{ id: "acct-anthropic", harness: "claude" }, CODEX_ACCOUNT, OTHER_CLAUDE_ACCOUNT];
    state.listeners.length = 0;
    setPendingAccount("agent-1", null);
  });

  it("is per chat, and cleared with null", () => {
    setPendingAccount("agent-1", "acct-openai");
    expect(getPendingAccountId("agent-1")).toBe("acct-openai");
    expect(getPendingAccountId("agent-2")).toBeNull();
    setPendingAccount("agent-1", null);
    expect(getPendingAccountId("agent-1")).toBeNull();
  });

  it("makes the next send a switch only for a signed-in account on another harness", () => {
    setPendingAccount("agent-1", "acct-openai");
    expect(pendingSwitchTarget("agent-1")?.id).toBe("acct-openai");

    // The chat's own account, or one on its own harness (a rebind, which a later phase adds).
    setPendingAccount("agent-1", "acct-anthropic");
    expect(pendingSwitchTarget("agent-1")).toBeNull();
    setPendingAccount("agent-1", OTHER_CLAUDE_ACCOUNT.id);
    expect(pendingSwitchTarget("agent-1")).toBeNull();

    // An account that was signed out since, or a chat the app no longer lists.
    setPendingAccount("agent-1", "acct-gone");
    expect(pendingSwitchTarget("agent-1")).toBeNull();
    setPendingAccount("agent-1", "acct-openai");
    state.chat = null;
    expect(pendingSwitchTarget("agent-1")).toBeNull();
  });

  it("clears itself once the chat runs on the pending account", () => {
    trackPendingLaneSettlement();
    setPendingAccount("agent-1", "acct-openai");
    const [listener] = state.listeners;
    listener([chatSnapshotFixture("agent-1", { active_agent: { harness: "claude", account_id: "acct-anthropic" } })]);
    expect(getPendingAccountId("agent-1")).toBe("acct-openai");
    listener([chatSnapshotFixture("agent-1", { active_agent: { harness: "codex", account_id: "acct-openai" } })]);
    expect(getPendingAccountId("agent-1")).toBeNull();
  });
});
