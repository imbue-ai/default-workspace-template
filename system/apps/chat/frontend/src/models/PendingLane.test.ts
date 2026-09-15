import { beforeEach, describe, expect, it, vi } from "vitest";

// The pending lane reads the chat and the accounts through their models; both are served from
// mutable holders here, and the listeners the settlement installs are captured to be driven.
const state = vi.hoisted(() => ({
  chat: null as unknown,
  accounts: [] as { id: string; harness: string; lane: string }[],
  listeners: [] as ((chats: unknown[]) => void)[],
  agentListeners: [] as ((chatId: string, previous: string, current: string) => void)[],
}));
vi.mock("./Chats", () => ({
  getChatById: () => state.chat ?? undefined,
  addChatsUpdatedListener: (listener: (chats: unknown[]) => void) => state.listeners.push(listener),
  addActiveAgentChangedListener: (listener: (chatId: string, previous: string, current: string) => void) =>
    state.agentListeners.push(listener),
}));
vi.mock("./Providers", () => ({
  accountForAgent: (id?: string) => state.accounts.find((account) => account.id === id) ?? null,
}));

import { chatSnapshotFixture } from "./chatSnapshotFixture";
import type { ProviderAccount } from "./Providers";
import {
  getPendingAccountId,
  isSwitchTarget,
  pendingSwitchTarget,
  setPendingAccount,
  switchKind,
  trackPendingLaneSettlement,
} from "./PendingLane";

const OWN_ACCOUNT = { id: "acct-anthropic", harness: "claude", lane: "anthropic" };
const CODEX_ACCOUNT = { id: "acct-openai", harness: "codex", lane: "openai" };
const OTHER_CLAUDE_ACCOUNT = { id: "acct-anthropic-2", harness: "claude", lane: "anthropic" };
// Two lanes share the pi harness with different model sets, so a move between them replaces the agent.
const OPENROUTER_ACCOUNT = { id: "acct-openrouter", harness: "pi-coding", lane: "openrouter" };
const OPENCODE_GO_ACCOUNT = { id: "acct-opencode-go", harness: "pi-coding", lane: "opencode-go" };

describe("the pending lane", () => {
  beforeEach(() => {
    state.chat = chatSnapshotFixture("agent-1", { active_agent: { harness: "claude", account_id: "acct-anthropic" } });
    state.accounts = [OWN_ACCOUNT, CODEX_ACCOUNT, OTHER_CLAUDE_ACCOUNT, OPENROUTER_ACCOUNT, OPENCODE_GO_ACCOUNT];
    state.listeners.length = 0;
    state.agentListeners.length = 0;
    setPendingAccount("agent-1", null);
  });

  it("is per chat, and cleared with null", () => {
    setPendingAccount("agent-1", "acct-openai");
    expect(getPendingAccountId("agent-1")).toBe("acct-openai");
    expect(getPendingAccountId("agent-2")).toBeNull();
    setPendingAccount("agent-1", null);
    expect(getPendingAccountId("agent-1")).toBeNull();
  });

  it("makes the next send a switch for any signed-in account but the chat's own", () => {
    const chat = chatSnapshotFixture("agent-1", { active_agent: { harness: "claude", account_id: "acct-anthropic" } });
    expect(isSwitchTarget(chat, CODEX_ACCOUNT as ProviderAccount)).toBe(true);
    expect(isSwitchTarget(chat, OTHER_CLAUDE_ACCOUNT as ProviderAccount)).toBe(true);
    expect(isSwitchTarget(chat, OWN_ACCOUNT as ProviderAccount)).toBe(false);

    setPendingAccount("agent-1", "acct-openai");
    expect(pendingSwitchTarget("agent-1")?.id).toBe("acct-openai");
    setPendingAccount("agent-1", OTHER_CLAUDE_ACCOUNT.id);
    expect(pendingSwitchTarget("agent-1")?.id).toBe(OTHER_CLAUDE_ACCOUNT.id);

    // The chat's own account is no switch.
    setPendingAccount("agent-1", "acct-anthropic");
    expect(pendingSwitchTarget("agent-1")).toBeNull();

    // An account that was signed out since, or a chat the app no longer lists.
    setPendingAccount("agent-1", "acct-gone");
    expect(pendingSwitchTarget("agent-1")).toBeNull();
    setPendingAccount("agent-1", "acct-openai");
    state.chat = null;
    expect(pendingSwitchTarget("agent-1")).toBeNull();
  });

  it("reads a move to the chat's own harness and lane as a rebind, anything else as a handoff", () => {
    const chat = chatSnapshotFixture("agent-1", { active_agent: { harness: "claude", account_id: "acct-anthropic" } });
    expect(switchKind(chat, OTHER_CLAUDE_ACCOUNT as ProviderAccount)).toBe("rebind");
    expect(switchKind(chat, CODEX_ACCOUNT as ProviderAccount)).toBe("handoff");

    const piChat = chatSnapshotFixture("agent-2", {
      active_agent: { harness: "pi-coding", account_id: "acct-openrouter" },
    });
    expect(switchKind(piChat, OPENCODE_GO_ACCOUNT as ProviderAccount)).toBe("handoff");
    expect(switchKind(piChat, { ...OPENROUTER_ACCOUNT, id: "acct-openrouter-2" } as ProviderAccount)).toBe("rebind");

    // A chat whose account is gone (or was never recorded) has no lane to match, so it is replaced.
    const unbound = chatSnapshotFixture("agent-3", { active_agent: { harness: "claude", account_id: null } });
    expect(switchKind(unbound, OTHER_CLAUDE_ACCOUNT as ProviderAccount)).toBe("handoff");
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

  it("clears itself once a rebind lands the same agent on the pending account", () => {
    trackPendingLaneSettlement();
    setPendingAccount("agent-1", OTHER_CLAUDE_ACCOUNT.id);
    const [listener] = state.listeners;
    listener([
      chatSnapshotFixture("agent-1", { active_agent: { harness: "claude", account_id: OTHER_CLAUDE_ACCOUNT.id } }),
    ]);
    expect(getPendingAccountId("agent-1")).toBeNull();
  });

  it("clears itself once the chat moved to a new agent, whatever account that agent runs on", () => {
    // A failed switch retried on a third account lands the chat there, not on the picked one.
    trackPendingLaneSettlement();
    setPendingAccount("agent-1", "acct-openai");
    setPendingAccount("agent-2", "acct-openai");
    const [agentListener] = state.agentListeners;
    agentListener("agent-1", "agent-1", "agent-1-successor");
    expect(getPendingAccountId("agent-1")).toBeNull();
    expect(getPendingAccountId("agent-2")).toBe("acct-openai");
    setPendingAccount("agent-2", null);
  });
});
