// @vitest-environment jsdom
/**
 * A render smoke test over every branch of the combo card.
 *
 * Replaces the same test written against the three-slot bar it succeeds, which is why the
 * assertions are phrased as behaviour rather than markup: what the user can see and click
 * should survive a faithful port, and it did not survive an unfaithful one.
 *
 * Rendered into a real DOM under jsdom. The card portals to <body> -- it lives inside
 * dockview's clipping overlay otherwise -- and mithril validates keyed fragments during the
 * DOM diff, not while building vnodes, so a vnode walk cannot see either.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

const agentState: { agent: ChatSnapshot | null } = { agent: null };
vi.mock("../models/Chats", () => ({
  getChatById: () => agentState.agent,
}));

const catalogState: { catalog: unknown } = { catalog: null };
vi.mock("../models/HarnessCatalog", () => ({
  ensureHarnessCatalogs: () => undefined,
  getHarnessCatalog: (harness?: string) => (harness === undefined ? null : catalogState.catalog),
}));

const settingsState: { choice: unknown } = { choice: null };
const picks: unknown[] = [];
vi.mock("../models/ModelSettings", () => ({
  effectiveChoice: () => settingsState.choice,
  changedAxes: () => ["model"],
  setModelChoice: (...args: unknown[]) => picks.push(args),
}));

// The workspace's chat settings as the page has them (null before the load), and every write
// the fast-limit row asked for.
const { DEFAULT_CHAT_SETTINGS, chatSettingsState, settingsWrites } = vi.hoisted(() => {
  const defaults = { fast_mode_default: "auto", fast_mode_turn_limit: 5, is_fast_mode_notice_shown: false };
  return {
    DEFAULT_CHAT_SETTINGS: defaults,
    chatSettingsState: { settings: defaults as typeof defaults | null, loads: 0 },
    settingsWrites: [] as unknown[],
  };
});
vi.mock("../models/ChatSettings", () => ({
  DEFAULT_CHAT_SETTINGS,
  getChatSettings: () => chatSettingsState.settings,
  ensureChatSettings: () => {
    chatSettingsState.loads += 1;
    return Promise.resolve(chatSettingsState.settings ?? DEFAULT_CHAT_SETTINGS);
  },
  updateChatSettings: (next: unknown) => {
    settingsWrites.push(next);
    return Promise.resolve(next);
  },
}));

// The chat's fast mode as the page has it (null before the load), the loads asked for, and every
// mode the chooser picked.
const { fastModeState, fastModeLoads, fastModeChoices } = vi.hoisted(() => ({
  fastModeState: { state: null as { mode: string; is_switched: boolean } | null },
  fastModeLoads: [] as string[],
  fastModeChoices: [] as [string, string][],
}));
vi.mock("../models/FastMode", () => ({
  getFastModeState: () => fastModeState.state,
  ensureFastModeState: (chatId: string) => {
    fastModeLoads.push(chatId);
    return Promise.resolve(fastModeState.state);
  },
  fastModeLabel: (state: { mode: string; is_switched: boolean }) =>
    state.mode === "off" ? "Off" : state.mode === "on" ? "On" : state.is_switched ? "Auto (off now)" : "Auto",
}));
vi.mock("./fast-mode-limit", () => ({
  chooseFastMode: (chatId: string, mode: string) => {
    fastModeChoices.push([chatId, mode]);
  },
}));
vi.mock("../models/Response", () => ({ getEventsForChat: () => [] }));

const providerState: { accounts: unknown[]; defaultId: string | null } = { accounts: [], defaultId: null };
// Every pin or unpin the star asked the server for, as (account id, pinned) pairs.
const pins: [string, boolean][] = [];
vi.mock("../models/Providers", () => ({
  getAccounts: () => providerState.accounts,
  getDefaultAccountId: () => providerState.defaultId,
  setDefaultAccount: (accountId: string, isDefault: boolean) => {
    pins.push([accountId, isDefault]);
    return Promise.resolve();
  },
  accountForAgent: (id?: string) => providerState.accounts.find((a) => (a as { id: string }).id === id) ?? null,
  openProviderChooser: () => undefined,
  deleteAccount: () => Promise.resolve(),
  renameAccount: () => Promise.resolve(),
  loadAccounts: () => Promise.resolve(),
}));

// The card's "start a chat on that provider" ask goes to the shell through chat/shell.ts.
const started: string[] = [];
vi.mock("../shell", () => ({
  startChatOnAccount: (accountId: string) => started.push(accountId),
  openSubagentTab: vi.fn(),
}));

// A press on another account hands the chat to the switch dialog (or an immediate switch); the
// armed card's Model row reopens it. Both recorded by account id.
const begun: string[] = [];
const reopened: string[] = [];
vi.mock("./SwitchDialog", () => ({
  beginSwitchTo: (_chatId: string, account: { id: string }) => begun.push(account.id),
  openSwitchDialog: (_chatId: string, account: { id: string }) => reopened.push(account.id),
}));

import m from "mithril";

import { hoverTooltipText } from "@imbue/workspace-ui/src/testing/tooltip";

import type { ChatSnapshot } from "../models/Chats";
import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import { getPendingAccountId, setPendingAccount, setPendingSwitch } from "../models/PendingLane";
import { ModelBar } from "./ModelBar";

const ROOT = () => document.getElementById("root") as HTMLElement;

function render(): void {
  m.render(ROOT(), m(ModelBar as never, { chatId: "a1" }));
}

/** Everything on screen, card and flyout included -- both portal out of the component. */
function screenText(): string {
  return `${ROOT().textContent ?? ""} ${document.body.textContent ?? ""}`;
}

function click(selector: string): void {
  const node = document.querySelector<HTMLElement>(selector);
  if (node === null) throw new Error(`no ${selector} on screen`);
  node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  render();
}

const OPUS = {
  id: "opus",
  label: "Opus",
  efforts: [],
  supports_fast: false,
  in_picker: true,
  harness_reported_model_id: null,
};
const ACCOUNT = {
  id: "acct-1",
  lane: "anthropic",
  harness: "claude",
  provider: "Anthropic",
  harness_label: "Claude Code",
  name: "",
  seq: 1,
  label: "Anthropic (Claude Code)",
};

function catalogOf(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    switch_mode: "eager_then_reconcile",
    picker_mode: "list",
    options: [OPUS],
    native_atomic_shoulder_tap_possible: true,
    popups: [],
    ...overrides,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

beforeEach(() => {
  document.body.innerHTML = '<div id="root"></div>';
  fastModeState.state = { mode: "auto", is_switched: false };
  fastModeLoads.length = 0;
  fastModeChoices.length = 0;
  picks.length = 0;
  started.length = 0;
  begun.length = 0;
  reopened.length = 0;
  pins.length = 0;
  settingsWrites.length = 0;
  chatSettingsState.settings = DEFAULT_CHAT_SETTINGS;
  chatSettingsState.loads = 0;
  providerState.defaultId = null;
  agentState.agent = chatSnapshotFixture("a1", { active_agent: { harness: "claude", account_id: "acct-1" } });
  catalogState.catalog = catalogOf();
  settingsState.choice = { identity: { model_id: "opus", effort: null, fast: false }, matched: OPUS, pending: null };
  providerState.accounts = [ACCOUNT];
  setPendingAccount("a1", null);
});

describe("the combo card", () => {
  it("renders nothing when the agent is unknown", () => {
    agentState.agent = null;
    render();
    expect(ROOT().innerHTML).toBe("");
  });

  it("shows the model on the trigger, and opens the card on click", () => {
    render();
    expect(screenText()).toContain("Opus");
    click(".model-selector-trigger");
    const text = screenText();
    expect(text).toContain("Provider");
    expect(text).toContain("Anthropic");
    expect(text).toContain("Claude Code");
  });

  it("still names the provider when there is no model to show", () => {
    // The three no-model states -- catalog not loaded, choice unresolved, no matching option.
    // A provider belongs to the ACCOUNT, so it survives all of them; only Model/Effort/Fast go.
    settingsState.choice = null;
    render();
    click(".model-selector-trigger");
    const text = screenText();
    expect(text).toContain("Anthropic");
    expect(text).not.toContain("Model");
  });

  it("renders a read-only harness without an effort control", () => {
    // agy: its `/model` is an interactive TUI with no scriptable form, so a picker there
    // offers a switch that cannot work.
    const withEffort = {
      ...OPUS,
      efforts: [
        { level: "low", in_picker: true },
        { level: "high", in_picker: true },
      ],
    };
    catalogState.catalog = catalogOf({ switch_mode: "read_only", options: [withEffort] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "low", fast: false },
      matched: withEffort,
      pending: null,
    };
    render();
    click(".model-selector-trigger");
    expect(document.querySelector<HTMLInputElement>('input[type="range"]')?.disabled).toBe(true);
  });

  it("stops explaining a read-only harness once its catalog says the model can be switched", () => {
    vi.useFakeTimers();
    catalogState.catalog = catalogOf({ switch_mode: "read_only" });
    render();
    click(".model-selector-trigger");
    const modelRow = document.querySelector<HTMLElement>('[data-card-row="model"]')!;
    expect(hoverTooltipText(modelRow)).toContain("run /model or /effort");

    catalogState.catalog = catalogOf();
    render();
    // The card stays open and mithril patches the row rather than replacing it, so the row
    // keeps whatever its first render attached.
    expect(document.querySelector('[data-card-row="model"]')).toBe(modelRow);
    expect(hoverTooltipText(modelRow)).toBeNull();
  });

  it("renders an effort slider only when there is more than one stop", () => {
    render();
    click(".model-selector-trigger");
    expect(document.querySelector('input[type="range"]')).toBeNull();

    // pi's non-reasoning models declare exactly ("off",). A one-stop slider is immovable and
    // painted full -- it looks broken and says the opposite of the truth.
    const oneStop = { ...OPUS, efforts: [{ level: "off", in_picker: true }] };
    catalogState.catalog = catalogOf({ options: [oneStop] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "off", fast: false },
      matched: oneStop,
      pending: null,
    };
    document.body.innerHTML = '<div id="root"></div>';
    render();
    click(".model-selector-trigger");
    expect(document.querySelector('input[type="range"]')).toBeNull();

    const twoStops = {
      ...OPUS,
      efforts: [
        { level: "low", in_picker: true },
        { level: "high", in_picker: true },
      ],
    };
    catalogState.catalog = catalogOf({ options: [twoStops] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "low", fast: false },
      matched: twoStops,
      pending: null,
    };
    document.body.innerHTML = '<div id="root"></div>';
    render();
    click(".model-selector-trigger");
    expect(document.querySelector('input[type="range"]')).not.toBeNull();
  });

  it("commits an effort on release, not on every notch of the drag", () => {
    // Each notch is a live switch typed into the agent's pane, and setModelChoice chains
    // rather than debounces -- a low-to-max drag would queue one per stop.
    const efforts = [
      { level: "low", in_picker: true },
      { level: "medium", in_picker: true },
      { level: "high", in_picker: true },
    ];
    const model = { ...OPUS, efforts };
    catalogState.catalog = catalogOf({ options: [model] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "low", fast: false },
      matched: model,
      pending: null,
    };
    render();
    click(".model-selector-trigger");
    const slider = document.querySelector<HTMLInputElement>('input[type="range"]');
    if (slider === null) throw new Error("no slider");

    slider.value = "1";
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    slider.value = "2";
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    expect(picks).toHaveLength(0);

    slider.dispatchEvent(new Event("change", { bubbles: true }));
    expect(picks).toHaveLength(1);
  });

  it("names the stop under the thumb while the drag is in flight, without committing it", () => {
    // Uncommitted is not the same as unshown: the row is what you are aiming with, so it reads
    // off the thumb from the first notch. The TRIGGER keeps saying the committed level, since
    // that is still what the agent is running on until release.
    const efforts = [
      { level: "low", in_picker: true },
      { level: "medium", in_picker: true },
      { level: "high", in_picker: true },
    ];
    const model = { ...OPUS, efforts };
    catalogState.catalog = catalogOf({ options: [model] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "low", fast: false },
      matched: model,
      pending: null,
    };
    render();
    click(".model-selector-trigger");
    const slider = document.querySelector<HTMLInputElement>('input[type="range"]');
    if (slider === null) throw new Error("no slider");
    const row = (): string => document.querySelector('[data-card-row="effort"]')?.textContent ?? "";
    expect(row()).toContain("Low");

    slider.value = "1";
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    render();
    expect(row()).toContain("Medium");
    expect(slider.value).toBe("1");
    // The green track follows too. A label that moved while the fill stayed put would just be
    // a differently broken row -- the three read as one control or none of them do.
    expect(slider.getAttribute("style")).toContain("50%");
    expect(picks).toHaveLength(0);
    expect(document.querySelector(".model-selector-trigger")?.textContent).toContain("Low");
  });

  it("goes back to naming the committed level once the drag is released", () => {
    // `onchange` clears the dragged index, and nothing has re-entered the card with a new
    // choice yet -- so the label has to fall back to the value rather than blank or stick.
    const efforts = [
      { level: "low", in_picker: true },
      { level: "high", in_picker: true },
    ];
    const model = { ...OPUS, efforts };
    catalogState.catalog = catalogOf({ options: [model] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "low", fast: false },
      matched: model,
      pending: null,
    };
    render();
    click(".model-selector-trigger");
    const slider = document.querySelector<HTMLInputElement>('input[type="range"]');
    if (slider === null) throw new Error("no slider");

    slider.value = "1";
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    slider.dispatchEvent(new Event("change", { bubbles: true }));
    render();
    expect(picks).toHaveLength(1);
    expect(document.querySelector('[data-card-row="effort"]')?.textContent).toContain("Low");
  });

  it("keeps naming a hidden level while the thumb sits at the far left", () => {
    // claude's `ultra` is not in the picker, so there is no stop for it and the thumb pins to
    // 0. The label comes from the VALUE, so it still says what the agent is actually on --
    // this is what the drag-follow must not trample.
    const efforts = [
      { level: "low", in_picker: true },
      { level: "high", in_picker: true },
      { level: "ultra", in_picker: false },
    ];
    const model = { ...OPUS, efforts };
    catalogState.catalog = catalogOf({ options: [model] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "ultra", fast: false },
      matched: model,
      pending: null,
    };
    render();
    click(".model-selector-trigger");
    expect(document.querySelector('[data-card-row="effort"]')?.textContent).toContain("Ultra");
    expect(document.querySelector<HTMLInputElement>('input[type="range"]')?.value).toBe("0");
  });

  it("hands a press on another harness's account to the switch dialog, and takes an armed choice back on a second press", () => {
    // The dialog (or, for a chat with no user turn, an immediate switch) decides what happens;
    // the card itself arms nothing and closes so the dialog has the screen.
    providerState.accounts = [
      ACCOUNT,
      { ...ACCOUNT, id: "acct-2", provider: "Google", harness: "antigravity", label: "Google (Antigravity CLI)" },
    ];
    render();
    click(".model-selector-trigger");
    click('[data-card-row="providers"]');
    const rows = [...document.querySelectorAll("button")].filter((b) => (b.textContent ?? "").includes("Google"));
    expect(rows).toHaveLength(1);
    rows[0].dispatchEvent(new MouseEvent("click", { bubbles: true }));
    render();
    expect(begun).toEqual(["acct-2"]);
    expect(getPendingAccountId("a1")).toBeNull();
    expect(started).toEqual([]);
    expect(document.querySelector('[data-model-popover="flyout"]')).toBeNull();
    expect(document.querySelector('[data-model-popover="card"]')).toBeNull();

    // Armed (what the dialog's "Switch this chat" does), the row wears its badge and pressing it
    // again takes the choice back without a second dialog.
    setPendingAccount("a1", "acct-2");
    render();
    click(".model-selector-trigger");
    click('[data-card-row="providers"]');
    const flyout = document.querySelector('[data-model-popover="flyout"]');
    const badged = [...(flyout?.querySelectorAll("button") ?? [])].find((b) =>
      (b.textContent ?? "").includes("Google"),
    );
    expect(badged?.querySelector(".account-row-badge")?.textContent).toBe("next");
    badged?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    render();
    expect(getPendingAccountId("a1")).toBeNull();
    expect(begun).toEqual(["acct-2"]);

    // So does pressing the account the chat already runs on: staying put is the choice then.
    setPendingAccount("a1", "acct-2");
    click('[data-card-row="providers"]');
    const own = [...document.querySelectorAll('[data-model-popover="flyout"] button')].find((b) =>
      (b.textContent ?? "").includes("Anthropic"),
    );
    if (own === undefined) throw new Error("no row for the chat's own account");
    own.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    render();
    expect(getPendingAccountId("a1")).toBeNull();
    expect(document.querySelector('[data-model-popover="flyout"]')).toBeNull();
    expect(started).toEqual([]);
  });

  it("hands an account on the chat's own harness to the dialog too, now that a chat can change account in place", () => {
    providerState.accounts = [
      ACCOUNT,
      { ...ACCOUNT, id: "acct-2", provider: "Anthropic 2", label: "Anthropic 2 (Claude Code)" },
    ];
    render();
    click(".model-selector-trigger");
    click('[data-card-row="providers"]');
    const rows = [...document.querySelectorAll("button")].filter((b) => (b.textContent ?? "").includes("Anthropic 2"));
    expect(rows).toHaveLength(1);
    rows[0].dispatchEvent(new MouseEvent("click", { bubbles: true }));
    render();
    expect(begun).toEqual(["acct-2"]);
    expect(started).toEqual([]);
    expect(document.querySelector('[data-model-popover="flyout"]')).toBeNull();
  });

  it("reads as the target while a switch is armed, and its Model row reopens the dialog", () => {
    const google = {
      ...ACCOUNT,
      id: "acct-2",
      provider: "Google",
      harness: "antigravity",
      harness_label: "Antigravity CLI",
      label: "Google (Antigravity CLI)",
    };
    providerState.accounts = [ACCOUNT, google];
    setPendingSwitch("a1", "acct-2", {
      identity: { model_id: "gemini", effort: "high", fast: false },
      label: "Gemini · High",
    });
    render();
    // The chip states the pick with a "next" mark rather than the current agent's model.
    expect(ROOT().textContent).toContain("Gemini · High");
    expect(ROOT().textContent).toContain("next");
    expect(ROOT().textContent).not.toContain("Opus");
    click(".model-selector-trigger");
    expect(document.querySelector('[data-card-row="providers"]')?.textContent).toContain("Google");
    expect(document.querySelector('[data-card-row="providers"]')?.textContent).toContain("after your next message");
    expect(document.querySelector('[data-card-row="model"]')?.textContent).toContain("Gemini · High");
    // The current agent's effort and fast rows are not the target's: they are not offered.
    expect(document.querySelector('[data-card-row="effort"]')).toBeNull();
    click('[data-card-row="model"]');
    expect(reopened).toEqual(["acct-2"]);
    expect(document.querySelector('[data-model-popover="card"]')).toBeNull();
    setPendingAccount("a1", null);
  });

  it("stars the default account and pins another on a press of its star", () => {
    providerState.accounts = [ACCOUNT, { ...ACCOUNT, id: "acct-2", provider: "Google", harness: "antigravity" }];
    providerState.defaultId = "acct-1";
    render();
    click(".model-selector-trigger");
    click('[data-card-row="providers"]');
    const pinned = document.querySelector('[aria-label="Stop opening new chats on Anthropic by default"]');
    expect(pinned?.getAttribute("aria-pressed")).toBe("true");
    const other = document.querySelector<HTMLElement>('[aria-label="Open new chats on Google by default"]');
    expect(other?.getAttribute("aria-pressed")).toBe("false");
    other?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(pins).toEqual([["acct-2", true]]);
    // Pressing the star is not pressing the row: no launch prompt, nothing started.
    render();
    expect(screenText()).not.toContain("Launch a new chat?");
    expect(started).toEqual([]);
  });

  it("confirms a sign-out in a dialog, and closing the card takes the dialog with it", () => {
    // The bin only appears on hover and signing out cannot be undone, so a single click would
    // too often be someone finding out what it was.
    render();
    click(".model-selector-trigger");
    click('[data-card-row="providers"]');
    expect(screenText()).not.toContain("Remove account");
    click('[aria-label="Sign out of Anthropic"]');
    expect(screenText()).toContain("Remove account");

    // Closing and reopening must not leave the confirmation up.
    click(".model-selector-trigger");
    click(".model-selector-trigger");
    click('[data-card-row="providers"]');
    expect(screenText()).not.toContain("Remove account");
  });

  it("states model, effort and fast on the trigger, from the card's own values", () => {
    // The chip is a SUMMARY of the card. Reading them off different sources is how they came
    // to disagree, so this pins them to one.
    const efforts = [
      { level: "low", in_picker: true },
      { level: "high", in_picker: true },
    ];
    const model = { ...OPUS, efforts, supports_fast: true };
    catalogState.catalog = catalogOf({ options: [model] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "high", fast: true },
      matched: model,
      pending: null,
    };
    render();
    const trigger = document.querySelector(".model-selector-trigger") as HTMLElement;
    expect(trigger.textContent).toContain("Opus");
    expect(trigger.textContent).toContain("High");
    expect(trigger.querySelector("svg")).not.toBeNull();
  });

  it("states the chat's fast mode on the fast row and opens the chooser from it", () => {
    // The row says which of the three modes the chat is in rather than showing a switch, since
    // auto is neither on nor off; pressing it closes the card and opens the chooser modal, where
    // the modes are picked and auto's turn limit lives.
    const model = { ...OPUS, supports_fast: true };
    catalogState.catalog = catalogOf({ options: [model] });
    settingsState.choice = { identity: { model_id: "opus", effort: null, fast: true }, matched: model, pending: null };
    chatSettingsState.settings = {
      fast_mode_default: "auto",
      fast_mode_turn_limit: 3,
      is_fast_mode_notice_shown: true,
    };
    fastModeState.state = { mode: "auto", is_switched: true };
    render();
    click(".model-selector-trigger");
    const row = document.querySelector<HTMLElement>('[data-card-row="fast"]');
    if (row === null) throw new Error("no fast row");
    expect(row.textContent).toContain("Fast Mode");
    expect(row.textContent).toContain("Auto (off now)");
    expect(document.querySelector(".fast-limit-input")).toBeNull();

    click('[data-card-row="fast"]');
    const modal = document.querySelector<HTMLElement>('[data-e2e="fast-mode-modal"]');
    if (modal === null) throw new Error("no fast-mode modal");
    expect(document.querySelector('[data-card-row="fast"]')).toBeNull();
    expect(modal.querySelector('[data-fast-mode="auto"]')?.getAttribute("aria-checked")).toBe("true");
    expect(modal.textContent).toContain("Fast for the first 3 turns, then standard speed.");
    const limit = modal.querySelector<HTMLInputElement>(".fast-limit-input");
    if (limit === null) throw new Error("no turn-limit field under Auto");
    expect(limit.value).toBe("3");

    click('[data-fast-mode="on"]');
    expect(fastModeChoices).toEqual([["a1", "on"]]);
    click(".fast-mode-done");
    expect(document.querySelector('[data-e2e="fast-mode-modal"]')).toBeNull();
  });

  it("asks for the chat's fast mode and shows the row unresolved until it is known", () => {
    const model = { ...OPUS, supports_fast: true };
    catalogState.catalog = catalogOf({ options: [model] });
    settingsState.choice = { identity: { model_id: "opus", effort: null, fast: true }, matched: model, pending: null };
    fastModeState.state = null;
    render();
    click(".model-selector-trigger");
    const row = document.querySelector<HTMLElement>('[data-card-row="fast"]');
    if (row === null) throw new Error("no fast row");
    expect(row.textContent).toContain("...");
    expect(fastModeLoads).toContain("a1");
  });

  it("gives a read-only harness no model list to open", () => {
    // agy's `/model` is an interactive TUI with no scriptable form. A chevron on that row
    // would be a promise the card cannot keep.
    catalogState.catalog = catalogOf({ switch_mode: "read_only" });
    render();
    click(".model-selector-trigger");
    expect(document.querySelector('[data-card-row="model"]')?.querySelector("svg")).toBeNull();
    click('[data-card-row="model"]');
    expect(document.querySelector('[data-model-popover="flyout"]')).toBeNull();
  });

  it("survives a dynamic harness with no static options", () => {
    // codex: its options are per-account and come from its own daemon, so the static catalog
    // is empty by design and the flyout must not throw on it.
    catalogState.catalog = catalogOf({ picker_mode: "dynamic", switch_mode: "on_change", options: [] });
    settingsState.choice = {
      identity: { model_id: "gpt-5", effort: null, fast: false },
      matched: null,
      pending: null,
    };
    expect(() => {
      render();
      click(".model-selector-trigger");
    }).not.toThrow();
  });
});
