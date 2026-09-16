// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

// The dialog reads the chat, its transcript, and the accounts through their models, and acts
// through the switch route, the shell, and the composer; every one is served from here so a test
// reads what the dialog did off plain lists.
const state = vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
  return {
    chat: null as unknown,
    events: [] as unknown[],
    accounts: [] as { id: string; harness: string; lane: string; label: string }[],
    options: [] as unknown[],
    switches: [] as unknown[][],
    started: [] as unknown[][],
    notices: [] as unknown[],
    draft: "",
    prepended: [] as string[],
  };
});
vi.mock("../models/Chats", () => ({
  getChatById: () => state.chat ?? undefined,
  addChatsUpdatedListener: () => undefined,
  addActiveAgentChangedListener: () => undefined,
}));
vi.mock("../models/Providers", () => ({
  accountForAgent: (id?: string) => state.accounts.find((account) => account.id === id) ?? null,
}));
vi.mock("../models/Response", () => ({
  getEventsForChat: () => state.events,
  mintMessageId: () => "m-1",
}));
vi.mock("../models/Handoffs", () => ({
  switchChat: vi.fn((...args: unknown[]) => {
    state.switches.push(args);
    return Promise.resolve({ kind: "handoff", phase: "summarizing", returned_block: "" });
  }),
}));
vi.mock("../shell", () => ({
  startChatOnAccount: (...args: unknown[]) => {
    state.started.push(args);
    return Promise.resolve(true);
  },
}));
vi.mock("./MessageInput", () => ({
  takeComposerDraft: () => {
    const draft = state.draft;
    state.draft = "";
    return draft;
  },
  prependToComposer: (_chatId: string, block: string) => state.prepended.push(block),
  raiseFailureNotice: (_chatId: string, notice: unknown) => state.notices.push(notice),
}));
vi.mock("../models/AccountModelOptions", () => ({
  fetchAccountModelOptions: () => Promise.resolve(state.options),
}));

import m from "mithril";
import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import { getPendingAccountId, getPendingPick, setPendingAccount } from "../models/PendingLane";
import type { ProviderAccount } from "../models/Providers";
import { SwitchDialog, beginSwitchTo } from "./SwitchDialog";

const OWN = { id: "acct-anthropic", harness: "claude", lane: "anthropic", label: "Anthropic (Claude Code)" };
const CODEX = { id: "acct-openai", harness: "codex", lane: "openai", label: "OpenAI (Codex)" };
const OTHER_CLAUDE = {
  id: "acct-anthropic-2",
  harness: "claude",
  lane: "anthropic",
  label: "Anthropic 2 (Claude Code)",
};
const ASTRA = {
  id: "gpt-6-astra",
  label: "GPT-6 Astra",
  efforts: [
    { level: "low", in_picker: true },
    { level: "high", in_picker: true },
  ],
  supports_fast: true,
  in_picker: true,
  harness_reported_model_id: null,
};
const WELCOME = { type: "user_message", event_id: "u-w", content: "/welcome", display: "hidden", timestamp: "t1" };
const TYPED = { type: "user_message", event_id: "u-1", content: "hello", timestamp: "t2" };

const ROOT = () => document.getElementById("root") as HTMLElement;

function render(): void {
  m.render(ROOT(), m(SwitchDialog as never, { chatId: "agent-1" }));
}

async function flush(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

function pressButton(label: string): void {
  const button = [...document.querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
  if (button === undefined) throw new Error(`no button labelled ${label}`);
  button.click();
}

describe("the switch dialog", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>';
    state.chat = chatSnapshotFixture("agent-1", { active_agent: { harness: "claude", account_id: OWN.id } });
    state.events = [WELCOME, { type: "assistant_message", event_id: "a-1", timestamp: "t1" }, TYPED];
    state.accounts = [OWN, CODEX, OTHER_CLAUDE];
    state.options = [ASTRA];
    state.switches.length = 0;
    state.started.length = 0;
    state.notices.length = 0;
    state.prepended.length = 0;
    state.draft = "";
    setPendingAccount("agent-1", null);
  });

  it("switches a chat with no user turn at once, with no dialog and nothing to say", async () => {
    state.events = [WELCOME, { type: "assistant_message", event_id: "a-1", timestamp: "t1" }];
    beginSwitchTo("agent-1", CODEX as ProviderAccount);
    await flush();
    expect(state.switches).toEqual([["agent-1", "acct-openai", "", "m-1"]]);
    render();
    expect(ROOT().textContent).toBe("");
    expect(getPendingAccountId("agent-1")).toBeNull();
  });

  it("reports a refused immediate switch through the composer's notice", async () => {
    state.events = [WELCOME];
    const { switchChat } = await import("../models/Handoffs");
    vi.mocked(switchChat).mockRejectedValueOnce(new Error("no such account"));
    beginSwitchTo("agent-1", CODEX as ProviderAccount);
    await flush();
    expect(state.notices).toEqual([{ title: "Couldn't switch to OpenAI (Codex)", detail: "no such account" }]);
  });

  it("asks a chat with context, and arms the switch with the model picked", async () => {
    beginSwitchTo("agent-1", CODEX as ProviderAccount);
    expect(state.switches).toEqual([]);
    render();
    expect(ROOT().textContent).toContain("Switch to Codex?");
    expect(ROOT().textContent).toContain(
      "Claude wraps up what it is doing and hands the conversation to OpenAI (Codex)",
    );
    expect(ROOT().textContent).toContain("Loading models…");
    await flush();
    render();
    const model = ROOT().querySelector<HTMLSelectElement>("select.switch-dialog-model");
    if (model === null) throw new Error("no model picker");
    expect([...model.options].map((option) => option.textContent)).toEqual(["Default model", "GPT-6 Astra"]);
    model.value = "gpt-6-astra";
    model.dispatchEvent(new Event("change", { bubbles: true }));
    render();
    const effort = ROOT().querySelector<HTMLSelectElement>("select.switch-dialog-effort");
    if (effort === null) throw new Error("no effort picker");
    effort.value = "high";
    effort.dispatchEvent(new Event("change", { bubbles: true }));
    render();
    // The primary action arms the switch; nothing is sent until the next message.
    const primary = [...document.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Switch this chat");
    expect(primary?.className).toContain("btn--primary");
    pressButton("Switch this chat");
    expect(getPendingAccountId("agent-1")).toBe("acct-openai");
    expect(getPendingPick("agent-1")).toEqual({
      identity: { model_id: "gpt-6-astra", effort: "high", fast: false },
      label: "GPT-6 Astra · High",
    });
    expect(state.switches).toEqual([]);
    render();
    expect(ROOT().textContent).toBe("");
  });

  it("starts a new chat on the target with the draft and the pick, leaving this chat alone", async () => {
    state.draft = "moving house";
    beginSwitchTo("agent-1", CODEX as ProviderAccount);
    render();
    await flush();
    render();
    const model = ROOT().querySelector<HTMLSelectElement>("select.switch-dialog-model");
    if (model === null) throw new Error("no model picker");
    model.value = "gpt-6-astra";
    model.dispatchEvent(new Event("change", { bubbles: true }));
    render();
    const secondary = [...document.querySelectorAll("button")].find(
      (b) => b.textContent?.trim() === "Start a new chat",
    );
    expect(secondary?.className).toContain("btn--secondary");
    pressButton("Start a new chat");
    await flush();
    expect(state.started).toEqual([
      ["acct-openai", "moving house", { model_id: "gpt-6-astra", effort: "low", fast: false }],
    ]);
    expect(getPendingAccountId("agent-1")).toBeNull();
    expect(state.switches).toEqual([]);
    render();
    expect(ROOT().textContent).toBe("");
  });

  it("arms a rebind at once, with no dialog and no pick, for an account on the chat's own harness and lane", () => {
    beginSwitchTo("agent-1", OTHER_CLAUDE as ProviderAccount);
    render();
    expect(ROOT().textContent).toBe("");
    // Armed rather than run: the agent keeps its conversation, and the next send restarts it, so
    // a turn in progress is not cut short by the press.
    expect(state.switches).toEqual([]);
    expect(getPendingAccountId("agent-1")).toBe("acct-anthropic-2");
    expect(getPendingPick("agent-1")).toBeNull();
  });

  it("offers only the default when the target has no models to offer", async () => {
    state.options = [];
    beginSwitchTo("agent-1", CODEX as ProviderAccount);
    render();
    await flush();
    render();
    expect(ROOT().querySelector("select")).toBeNull();
    expect(ROOT().textContent).toContain("Codex starts on its default model");
    pressButton("Switch this chat");
    expect(getPendingPick("agent-1")).toBeNull();
  });
});
