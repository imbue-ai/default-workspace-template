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
    optionsError: null as string | null,
    switches: [] as unknown[][],
    started: [] as unknown[][],
    notices: [] as unknown[],
    draft: "",
    draftAttachments: [] as unknown[],
    isStartAccepted: true,
    isTranscriptLoaded: true,
    restored: [] as unknown[],
  };
});
vi.mock("../models/Chats", () => ({
  getChatById: () => state.chat ?? undefined,
  addChatsUpdatedListener: () => undefined,
  addActiveAgentChangedListener: () => undefined,
}));
vi.mock("../models/HarnessCatalog", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/HarnessCatalog")>()),
  getHarnessCatalog: (await import("../models/harnessCatalogFixture")).harnessCatalogFixture,
}));
vi.mock("../models/Providers", () => ({
  accountForAgent: (id?: string) => state.accounts.find((account) => account.id === id) ?? null,
}));
vi.mock("../models/Response", () => ({
  getEventsForChat: () => state.events,
  isTranscriptLoaded: () => state.isTranscriptLoaded,
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
    return Promise.resolve(state.isStartAccepted);
  },
}));
vi.mock("./MessageInput", () => ({
  // The real one expands the text with the composer's attachment references and hands them back
  // together; the dialog must move both, not just the text.
  takeComposerDraft: () => {
    const taken = {
      text: state.draft,
      finalText: state.draftAttachments.length === 0 ? state.draft : `${state.draft} + attachments`,
      attachments: state.draftAttachments,
    };
    state.draft = "";
    state.draftAttachments = [];
    return Promise.resolve(taken);
  },
  restoreComposerDraft: (_chatId: string, taken: unknown) => state.restored.push(taken),
  raiseFailureNotice: (_chatId: string, notice: unknown) => state.notices.push(notice),
}));
vi.mock("../models/AccountModelOptions", () => ({
  fetchAccountModelOptions: () =>
    state.optionsError === null ? Promise.resolve(state.options) : Promise.reject(new Error(state.optionsError)),
}));

import m from "mithril";
import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import { getPendingAccountId, getPendingPick, setPendingAccount } from "../models/PendingLane";
import type { ProviderAccount } from "../models/Providers";
import { SwitchDialog, beginSwitchTo, closeSwitchDialog, openSwitchDialog } from "./SwitchDialog";

const OWN = { id: "acct-anthropic", harness: "claude", lane: "anthropic", label: "Anthropic (Claude Code)" };
const CODEX = { id: "acct-openai", harness: "codex", lane: "openai", label: "OpenAI (Codex)" };
const OTHER_CLAUDE = {
  id: "acct-anthropic-2",
  harness: "claude",
  lane: "anthropic",
  label: "Anthropic 2 (Claude Code)",
};
// agy's model bar is read-only: its model is changed from the agent's terminal, not from the chat.
const GOOGLE = { id: "acct-google", harness: "antigravity", lane: "google", label: "Google (Antigravity CLI)" };
const OTHER_GOOGLE = { ...GOOGLE, id: "acct-google-2", label: "Google 2 (Antigravity CLI)" };
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

/** Choose an option of one of the dialog's selects, as a user would. */
function choose(selectClass: string, value: string): void {
  const select = ROOT().querySelector<HTMLSelectElement>(`select.${selectClass}`);
  if (select === null) throw new Error(`no ${selectClass} picker`);
  select.value = value;
  select.dispatchEvent(new Event("change", { bubbles: true }));
  render();
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
    state.optionsError = null;
    state.switches.length = 0;
    state.started.length = 0;
    state.notices.length = 0;
    state.restored.length = 0;
    state.draft = "";
    state.draftAttachments = [];
    state.isStartAccepted = true;
    state.isTranscriptLoaded = true;
    setPendingAccount("agent-1", null);
    // The dialog is module state: a test that leaves it up would render into the next one's root.
    closeSwitchDialog();
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

  it("asks rather than switching at once when the transcript has not loaded", async () => {
    // The window is empty because nothing landed, not because the chat is new: acting on it would
    // switch a chat of any length with no dialog, no summary and no message.
    state.isTranscriptLoaded = false;
    state.events = [];
    beginSwitchTo("agent-1", CODEX as ProviderAccount);
    await flush();
    expect(state.switches).toEqual([]);
    render();
    expect(ROOT().textContent).toContain("Switch to Codex?");
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
      "Claude Code wraps up what it is doing and hands the conversation to OpenAI (Codex)",
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
      option: ASTRA,
    });
    expect(state.switches).toEqual([]);
    render();
    expect(ROOT().textContent).toBe("");
  });

  it("starts a new chat on the target with the draft and the pick, leaving this chat alone", async () => {
    state.draft = "moving house";
    // Attached in the old composer: what the new chat starts with is the expanded text, not the
    // bare draft, so the file is not left behind by the move.
    state.draftAttachments = [{ localId: "a1", fileName: "plan.pdf" }];
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
      ["acct-openai", "moving house + attachments", { model_id: "gpt-6-astra", effort: "low", fast: false }],
    ]);
    expect(getPendingAccountId("agent-1")).toBeNull();
    expect(state.switches).toEqual([]);
    render();
    expect(ROOT().textContent).toBe("");
  });

  it("hands the draft back, attachments and all, when the new chat could not be started", async () => {
    state.draft = "moving house";
    state.draftAttachments = [{ localId: "a1", fileName: "plan.pdf" }];
    state.isStartAccepted = false;
    beginSwitchTo("agent-1", CODEX as ProviderAccount);
    render();
    await flush();
    render();
    pressButton("Start a new chat");
    await flush();
    expect(state.restored).toEqual([
      {
        text: "moving house",
        finalText: "moving house + attachments",
        attachments: [{ localId: "a1", fileName: "plan.pdf" }],
      },
    ]);
  });

  it("says the models could not be loaded rather than that the harness has none", async () => {
    state.optionsError = "the daemon is not answering";
    beginSwitchTo("agent-1", CODEX as ProviderAccount);
    render();
    await flush();
    render();
    const text = ROOT().textContent ?? "";
    expect(text).toContain("Could not load Codex's models");
    expect(text).toContain("the daemon is not answering");
    // The switch itself needs no model, so the dialog stays usable.
    expect(text).toContain("Switch this chat");
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

  it("offers a rebind the model it keeps by default, arms another picked there, and shows that pick on reopening", async () => {
    // What the strip's "Change" opens for a rebind, which the provider menu armed without asking.
    openSwitchDialog("agent-1", OTHER_CLAUDE as ProviderAccount);
    render();
    expect(ROOT().textContent).toContain("Switch to Anthropic 2 (Claude Code)?");
    expect(ROOT().textContent).toContain(
      "Claude Code restarts on Anthropic 2 (Claude Code) and keeps this conversation, starting with your next message.",
    );
    await flush();
    render();
    const model = ROOT().querySelector<HTMLSelectElement>("select.switch-dialog-model");
    expect([...(model?.options ?? [])].map((option) => option.textContent)).toEqual([
      "Keep the current model",
      "GPT-6 Astra",
    ]);
    pressButton("Switch this chat");
    expect(getPendingAccountId("agent-1")).toBe("acct-anthropic-2");
    expect(getPendingPick("agent-1")).toBeNull();

    openSwitchDialog("agent-1", OTHER_CLAUDE as ProviderAccount);
    render();
    await flush();
    render();
    choose("switch-dialog-model", "gpt-6-astra");
    choose("switch-dialog-effort", "high");
    pressButton("Switch this chat");
    expect(getPendingPick("agent-1")).toEqual({
      identity: { model_id: "gpt-6-astra", effort: "high", fast: false },
      label: "GPT-6 Astra · High",
      option: ASTRA,
    });

    // Reopened, the dialog starts from what is armed, so confirming it again does not drop the pick.
    openSwitchDialog("agent-1", OTHER_CLAUDE as ProviderAccount);
    render();
    await flush();
    render();
    expect(ROOT().querySelector<HTMLSelectElement>("select.switch-dialog-model")?.value).toBe("gpt-6-astra");
    expect(ROOT().querySelector<HTMLSelectElement>("select.switch-dialog-effort")?.value).toBe("high");
    pressButton("Switch this chat");
    expect(getPendingPick("agent-1")?.identity).toEqual({ model_id: "gpt-6-astra", effort: "high", fast: false });
    expect(state.switches).toEqual([]);
  });

  it("starts a rebind's new chat on the model this chat runs on when nothing new is picked", async () => {
    state.chat = chatSnapshotFixture("agent-1", {
      active_agent: {
        harness: "claude",
        account_id: OWN.id,
        model_choice: {
          identity: { model_id: "claude-opus-5", effort: "high", fast: false },
          matched: { ...ASTRA, id: "opus[1m]", label: "Opus 5" },
        },
      },
    });
    state.draft = "a fresh start";
    openSwitchDialog("agent-1", OTHER_CLAUDE as ProviderAccount);
    render();
    await flush();
    render();
    pressButton("Start a new chat");
    await flush();
    // The live identity carries the raw id claude reports; a new chat is started on the catalog id it matched.
    expect(state.started).toEqual([
      ["acct-anthropic-2", "a fresh start", { model_id: "opus[1m]", effort: "high", fast: false }],
    ]);
  });

  it("starts a rebind's new chat with no pick on a harness whose model the chat app cannot switch", async () => {
    // agy's model is changed from the agent's terminal: carrying the chat's model over would be a pick the
    // new chat could never apply.
    state.accounts = [GOOGLE, OTHER_GOOGLE];
    state.options = [];
    state.chat = chatSnapshotFixture("agent-1", {
      active_agent: {
        harness: "antigravity",
        account_id: GOOGLE.id,
        model_choice: {
          identity: { model_id: "gemini-3-pro", effort: null, fast: false },
          matched: { ...ASTRA, id: "gemini-3-pro", label: "Gemini 3 Pro", efforts: [], supports_fast: false },
        },
      },
    });
    state.draft = "a fresh start";
    openSwitchDialog("agent-1", OTHER_GOOGLE as ProviderAccount);
    render();
    await flush();
    render();
    expect(ROOT().textContent).toContain("Switch to Google 2 (Antigravity CLI)?");
    pressButton("Start a new chat");
    await flush();
    expect(state.started).toEqual([["acct-google-2", "a fresh start", null]]);
  });

  it("points a harness the chat app cannot switch at its terminal rather than at the model bar", async () => {
    state.accounts = [GOOGLE, OTHER_GOOGLE];
    state.options = [];
    state.chat = chatSnapshotFixture("agent-1", {
      active_agent: { harness: "antigravity", account_id: GOOGLE.id },
    });
    openSwitchDialog("agent-1", OTHER_GOOGLE as ProviderAccount);
    render();
    await flush();
    render();
    expect(ROOT().querySelector("select")).toBeNull();
    expect(ROOT().textContent).toContain(
      "Antigravity CLI keeps its current model, which is changed from the agent's terminal, not from the chat.",
    );
  });

  it("says a rebind keeps its model when the account has none to offer", async () => {
    state.options = [];
    openSwitchDialog("agent-1", OTHER_CLAUDE as ProviderAccount);
    render();
    await flush();
    render();
    expect(ROOT().querySelector("select")).toBeNull();
    expect(ROOT().textContent).toContain("Claude Code keeps its current model; you can change it once it is running.");
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
