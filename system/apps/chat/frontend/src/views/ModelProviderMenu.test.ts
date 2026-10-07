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

const agentState: { agent: ChatSnapshot | null; provisional: { account_id: string } | undefined } = {
  agent: null,
  provisional: undefined,
};
vi.mock("../models/Chats", () => ({
  getChatById: () => agentState.agent ?? undefined,
  getProvisionalChat: () => agentState.provisional,
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
// the fast-limit row and the default switches asked for.
const { DEFAULT_CHAT_SETTINGS, chatSettingsState, settingsWrites } = vi.hoisted(() => {
  const defaults = {
    fast_mode_default: "auto",
    fast_mode_turn_limit: 2,
    is_fast_mode_notice_shown: false,
    autocompact_default: true,
    compaction_status_presentation: "both",
    is_autocompact_notice_shown: false,
  };
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
// Only the backend-backed half is faked; the words the rows read are the real ones, so what
// this asserts on is what the menu really says.
vi.mock("../models/FastMode", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/FastMode")>()),
  getFastModeState: () => fastModeState.state,
  ensureFastModeState: (chatId: string) => {
    fastModeLoads.push(chatId);
    return Promise.resolve(fastModeState.state);
  },
}));
vi.mock("./fast-mode-limit", () => ({
  chooseFastMode: (chatId: string, mode: string) => {
    fastModeChoices.push([chatId, mode]);
  },
}));
vi.mock("../models/Response", () => ({ getEventsForChat: () => [] }));

// The chat's auto-compact setting as the page has it (null before the load), the loads asked for,
// and every write the submenu made.
const { autocompactState, autocompactLoads, autocompactWrites } = vi.hoisted(() => ({
  autocompactState: { state: null as { is_enabled: boolean } | null },
  autocompactLoads: [] as string[],
  autocompactWrites: [] as [string, { is_enabled: boolean }][],
}));
vi.mock("../models/Autocompact", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Autocompact")>()),
  getAutocompactState: () => autocompactState.state,
  ensureAutocompactState: (chatId: string) => {
    autocompactLoads.push(chatId);
    return Promise.resolve(autocompactState.state);
  },
  setAutocompactState: (chatId: string, next: { is_enabled: boolean }) => {
    autocompactWrites.push([chatId, next]);
    return Promise.resolve(next);
  },
}));

const providerState: { accounts: unknown[]; defaultId: string | null; isLoaded: boolean } = {
  accounts: [],
  defaultId: null,
  isLoaded: true,
};
// Every time something opened the provider chooser, with whether it asked to hear of the sign-in.
const chooserOpens: { hasOnSignedIn: boolean }[] = [];
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
  accountForFirstSend: (id: string) =>
    providerState.accounts.find((a) => (a as { id: string }).id === id) ?? providerState.accounts[0] ?? null,
  areAccountsLoaded: () => providerState.isLoaded,
  isAccountSignedOut: (id?: string | null) =>
    !!id && providerState.isLoaded && !providerState.accounts.some((a) => (a as { id: string }).id === id),
  openProviderChooser: (intent: { onSignedIn?: unknown } = {}) =>
    chooserOpens.push({ hasOnSignedIn: intent.onSignedIn !== undefined }),
  deleteAccount: () => Promise.resolve(),
  renameAccount: () => Promise.resolve(),
  loadAccounts: () => Promise.resolve(),
}));

// The card's "start a chat on that provider" ask goes to the shell through chat/shell.ts.
const started: string[] = [];
vi.mock("../shell", () => ({
  startChatOnAccount: (accountId: string) => started.push(accountId),
  openSubagentView: vi.fn(),
}));

// A press on another account hands the chat to the switch dialog (or an immediate switch); the
// armed card's Model row reopens it. Both recorded by account id.
const begun: string[] = [];
const reopened: string[] = [];
vi.mock("./SwitchDialog", async (importOriginal) => ({
  takeBackSwitch: (await importOriginal<typeof import("./SwitchDialog")>()).takeBackSwitch,
  beginSwitchTo: (_chatId: string, account: { id: string }) => begun.push(account.id),
  openSwitchDialog: (_chatId: string, account: { id: string }) => reopened.push(account.id),
}));

import m from "mithril";

import { hoverTooltipText } from "@imbue/workspace-ui/src/testing/tooltip";

import type { ChatSnapshot } from "../models/Chats";
import { chatSnapshotFixture, handoffStateFixture, rebindStateFixture } from "../models/chatSnapshotFixture";
import { getPendingAccountId, setPendingAccount, setPendingSwitch, setSwitchSending } from "../models/PendingLane";
import { ModelProviderMenu } from "./ModelProviderMenu";
import * as css from "./modelProviderMenuStyles";

const ROOT = () => document.getElementById("root") as HTMLElement;

function render(): void {
  m.render(ROOT(), m(ModelProviderMenu as never, { chatId: "a1" }));
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

/** Take the pointer off the open submenu and wait out the menu's leave grace. Needs fake timers,
 *  and overshoots the shared menu's own grace constant rather than restating it. */
function leaveSubmenu(): void {
  const submenu = document.querySelector<HTMLElement>('[data-menu-part="submenu"]');
  if (submenu === null) throw new Error("no submenu to leave");
  submenu.dispatchEvent(new MouseEvent("mouseleave", { bubbles: true }));
  vi.advanceTimersByTime(1000);
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
    supports_compaction: false,
    can_interrupt_compaction: false,
    popups: [],
    ...overrides,
  };
}

/** Put the chat on a model that supports fast mode, so the menu offers its Fast mode row. */
function withFastModel(): void {
  const model = { ...OPUS, supports_fast: true };
  catalogState.catalog = catalogOf({ options: [model] });
  settingsState.choice = { identity: { model_id: "opus", effort: null, fast: true }, matched: model, pending: null };
}

afterEach(() => {
  vi.useRealTimers();
});

beforeEach(() => {
  document.body.innerHTML = '<div id="root"></div>';
  fastModeState.state = { mode: "auto", is_switched: false };
  fastModeLoads.length = 0;
  fastModeChoices.length = 0;
  autocompactState.state = { is_enabled: true };
  autocompactLoads.length = 0;
  autocompactWrites.length = 0;
  picks.length = 0;
  started.length = 0;
  begun.length = 0;
  reopened.length = 0;
  pins.length = 0;
  settingsWrites.length = 0;
  chatSettingsState.settings = DEFAULT_CHAT_SETTINGS;
  chatSettingsState.loads = 0;
  providerState.defaultId = null;
  providerState.isLoaded = true;
  chooserOpens.length = 0;
  agentState.provisional = undefined;
  agentState.agent = chatSnapshotFixture("a1", { active_agent: { harness: "claude", account_id: "acct-1" } });
  catalogState.catalog = catalogOf();
  settingsState.choice = { identity: { model_id: "opus", effort: null, fast: false }, matched: OPUS, pending: null };
  providerState.accounts = [ACCOUNT];
  setPendingAccount("a1", null);
  setSwitchSending("a1", false);
});

describe("the combo card", () => {
  it("holds the row with a loading chip for a chat the page knows nothing about yet", () => {
    agentState.agent = null;
    render();
    expect(ROOT().querySelector(".model-selector-loading")?.textContent).toBe("Loading…");
  });

  it("names the account a chat with no agent yet starts on, with no menu to open", () => {
    agentState.agent = null;
    agentState.provisional = { account_id: "" };
    render();
    expect(ROOT().querySelector(".model-selector-provisional")?.textContent).toBe("Anthropic");
    expect(ROOT().querySelector(".model-selector-trigger")).toBeNull();
  });

  it("says a chat with no agent yet is not connected when no provider is signed in, and opens the chooser", () => {
    agentState.agent = null;
    agentState.provisional = { account_id: "" };
    providerState.accounts = [];
    render();
    expect(screenText()).toContain("Not connected");
    click(".model-selector-not-connected");
    expect(chooserOpens).toEqual([{ hasOnSignedIn: false }]);
  });

  it("does not call a chat not connected before the account list has loaded", () => {
    providerState.isLoaded = false;
    providerState.accounts = [];
    agentState.agent = null;
    agentState.provisional = { account_id: "" };
    render();
    expect(screenText()).not.toContain("Not connected");
    expect(ROOT().querySelector(".model-selector-loading")).not.toBeNull();
    agentState.agent = chatSnapshotFixture("a1", { active_agent: { harness: "claude", account_id: "" } });
    settingsState.choice = null;
    render();
    expect(screenText()).not.toContain("Not connected");
    expect(ROOT().querySelector(".model-selector-loading")).not.toBeNull();
  });

  it("says a running chat with no provider signed in is not connected, and switches it onto the one signed in", () => {
    agentState.agent = chatSnapshotFixture("a1", { active_agent: { harness: "claude", account_id: "" } });
    settingsState.choice = null;
    providerState.accounts = [];
    render();
    expect(screenText()).toContain("Not connected");
    click(".model-selector-not-connected");
    expect(chooserOpens).toEqual([{ hasOnSignedIn: true }]);
  });

  it("keeps a chip under a running chat that names no account while providers are signed in", () => {
    // A chat whose agent is still connecting reads this way until its model arrives.
    agentState.agent = chatSnapshotFixture("a1", { active_agent: { harness: "claude", account_id: "" } });
    settingsState.choice = null;
    render();
    expect(ROOT().querySelector(".model-selector-trigger")?.textContent).toContain("Model");
    click(".model-selector-trigger");
    expect(screenText()).toContain("No account");
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

  it("shows no chip for a chat whose account was signed out, rather than the model it last ran", () => {
    // The composer's notice stands in its place, with the way to choose a provider.
    agentState.agent = chatSnapshotFixture("a1", { active_agent: { harness: "claude", account_id: "acct-gone" } });
    render();
    expect(ROOT().querySelector(".model-selector-trigger")).toBeNull();
    expect(screenText()).not.toContain("Opus");
  });

  it("brings the chip back for a signed-out chat once a provider is chosen for it", () => {
    agentState.agent = chatSnapshotFixture("a1", { active_agent: { harness: "claude", account_id: "acct-gone" } });
    setPendingAccount("a1", "acct-1");
    render();
    expect(ROOT().querySelector(".model-selector-trigger")?.textContent).toContain("next");
    setPendingAccount("a1", null);
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
    const modelRow = document.querySelector<HTMLElement>('[data-menu-row="model"]')!;
    expect(hoverTooltipText(modelRow)).toContain("run /model or /effort");

    catalogState.catalog = catalogOf();
    render();
    // The read-only value row gives way to the interactive submenu row; whatever element
    // stands in the slot now must explain nothing.
    const switched = document.querySelector<HTMLElement>('[data-menu-row="model"]')!;
    expect(hoverTooltipText(switched)).toBeNull();
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
    const row = (): string => document.querySelector('[data-menu-row="effort"]')?.textContent ?? "";
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
    expect(document.querySelector('[data-menu-row="effort"]')?.textContent).toContain("Low");
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
    expect(document.querySelector('[data-menu-row="effort"]')?.textContent).toContain("Ultra");
    expect(document.querySelector<HTMLInputElement>('input[type="range"]')?.value).toBe("0");
  });

  it("colours each tick for the part of the track it is drawn on", () => {
    const efforts = [
      { level: "low", in_picker: true },
      { level: "medium", in_picker: true },
      { level: "high", in_picker: true },
      { level: "xhigh", in_picker: true },
    ];
    const model = { ...OPUS, efforts };
    catalogState.catalog = catalogOf({ options: [model] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "high", fast: false },
      matched: model,
      pending: null,
    };
    render();
    click(".model-selector-trigger");
    const slider = document.querySelector<HTMLInputElement>('input[type="range"]');
    if (slider === null) throw new Error("no slider");
    // The wrap holds the tick layer and the input; the thumb's own stop (index 2) is not drawn.
    const ticks = [...(slider.parentElement?.firstElementChild?.children ?? [])];
    expect(ticks.map((tick) => tick.className)).toEqual([
      css.SLIDER_TICK_ON_FILL,
      css.SLIDER_TICK_ON_FILL,
      css.SLIDER_TICK_ON_TRACK,
    ]);
  });

  it("hands a press on another harness's account to the switch dialog, and takes an armed choice back on a second press", () => {
    // The dialog (or, for a chat with no user turn, an immediate switch) decides what happens;
    // the menu itself arms nothing and closes so the dialog has the screen.
    providerState.accounts = [
      ACCOUNT,
      { ...ACCOUNT, id: "acct-2", provider: "Google", harness: "antigravity", label: "Google (Antigravity CLI)" },
    ];
    render();
    click(".model-selector-trigger");
    click('[data-menu-row="providers"]');
    const rows = [...document.querySelectorAll("button")].filter((b) => (b.textContent ?? "").includes("Google"));
    expect(rows).toHaveLength(1);
    rows[0].dispatchEvent(new MouseEvent("click", { bubbles: true }));
    render();
    expect(begun).toEqual(["acct-2"]);
    expect(getPendingAccountId("a1")).toBeNull();
    expect(started).toEqual([]);
    expect(document.querySelector('[data-menu-part="submenu"]')).toBeNull();
    expect(document.querySelector('[data-menu-part="menu"]')).toBeNull();

    // Armed (what the dialog's "Switch this chat" does), the row wears its badge and pressing it
    // again takes the choice back without a second dialog.
    setPendingAccount("a1", "acct-2");
    render();
    click(".model-selector-trigger");
    click('[data-menu-row="providers"]');
    const flyout = document.querySelector('[data-menu-part="submenu"]');
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
    click('[data-menu-row="providers"]');
    const own = [...document.querySelectorAll('[data-menu-part="submenu"] button')].find((b) =>
      (b.textContent ?? "").includes("Anthropic"),
    );
    if (own === undefined) throw new Error("no row for the chat's own account");
    own.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    render();
    expect(getPendingAccountId("a1")).toBeNull();
    expect(document.querySelector('[data-menu-part="submenu"]')).toBeNull();
    expect(started).toEqual([]);
  });

  it("hands an account on the chat's own harness to the dialog too, now that a chat can change account in place", () => {
    providerState.accounts = [
      ACCOUNT,
      { ...ACCOUNT, id: "acct-2", provider: "Anthropic 2", label: "Anthropic 2 (Claude Code)" },
    ];
    render();
    click(".model-selector-trigger");
    click('[data-menu-row="providers"]');
    const rows = [...document.querySelectorAll("button")].filter((b) => (b.textContent ?? "").includes("Anthropic 2"));
    expect(rows).toHaveLength(1);
    rows[0].dispatchEvent(new MouseEvent("click", { bubbles: true }));
    render();
    expect(begun).toEqual(["acct-2"]);
    expect(started).toEqual([]);
    expect(document.querySelector('[data-menu-part="submenu"]')).toBeNull();
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
    // The current agent's model offers both rows, so their absence below is the armed switch
    // hiding them rather than this model having none to show.
    const choosy = {
      ...OPUS,
      efforts: [
        { level: "low", in_picker: true },
        { level: "high", in_picker: true },
      ],
      supports_fast: true,
    };
    catalogState.catalog = catalogOf({ options: [choosy] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "low", fast: false },
      matched: choosy,
      pending: null,
    };
    setPendingSwitch("a1", "acct-2", {
      identity: { model_id: "gemini", effort: "high", fast: false },
      label: "Gemini · High",
      option: { ...OPUS, id: "gemini", label: "Gemini" },
    });
    render();
    // The chip states the pick with a "next" mark rather than the current agent's model.
    expect(ROOT().textContent).toContain("Gemini · High");
    expect(ROOT().textContent).toContain("next");
    expect(ROOT().textContent).not.toContain("Opus");
    click(".model-selector-trigger");
    expect(document.querySelector('[data-menu-row="providers"]')?.textContent).toContain("Google");
    expect(document.querySelector('[data-menu-row="providers"]')?.textContent).toContain("next message");
    expect(document.querySelector('[data-menu-row="model"]')?.textContent).toContain("Gemini · High");
    // The current agent's effort and fast rows are not the target's: they are not offered.
    expect(document.querySelector('[data-menu-row="effort"]')).toBeNull();
    expect(document.querySelector('[data-menu-part="menu"]')?.textContent).not.toContain("Fast mode");
    click('[data-menu-row="model"] button');
    expect(reopened).toEqual(["acct-2"]);
    expect(document.querySelector('[data-menu-part="menu"]')).toBeNull();
  });

  it("reads an armed rebind as the agent's own model, with a Model row that opens the dialog to pick another", () => {
    const other = { ...ACCOUNT, id: "acct-2", provider: "Anthropic 2", label: "Anthropic 2 (Claude Code)" };
    providerState.accounts = [ACCOUNT, other];
    setPendingAccount("a1", "acct-2");
    render();
    // Nothing picked: the agent keeps its model, so that is what the next message runs on.
    expect(ROOT().textContent).toContain("Opus");
    expect(ROOT().textContent).toContain("next");
    click(".model-selector-trigger");
    expect(document.querySelector('[data-menu-row="providers"]')?.textContent).toContain("next message");
    expect(document.querySelector('[data-menu-row="model"]')?.textContent).toContain("Opus");
    click('[data-menu-row="model"] button');
    expect(reopened).toEqual(["acct-2"]);

    setPendingSwitch("a1", "acct-2", {
      identity: { model_id: "haiku", effort: "low", fast: false },
      label: "Haiku 4.5 · Low",
      option: { ...OPUS, id: "haiku", label: "Haiku 4.5" },
    });
    render();
    expect(ROOT().textContent).toContain("Haiku 4.5 · Low");
    expect(ROOT().textContent).not.toContain("Opus");
  });

  it("reads the switch a reloaded page finds under way, which its own lane cannot name", () => {
    // A page reloaded mid-switch has no armed lane: the chat carries the pick, and the live choice
    // will not name it until the harness has taken it, so the chip reads the chat.
    agentState.agent = chatSnapshotFixture("a1", {
      active_agent: { harness: "claude", account_id: "acct-1" },
      handoff: rebindStateFixture({ model_pick: { model_id: "opus", effort: "high", fast: false } }),
    });
    render();
    expect(ROOT().textContent).toContain("Opus · High");
    // Its message has gone: the switch is being carried out, not waiting on the next one.
    expect(ROOT().textContent).not.toContain("next");
  });

  describe("once the armed switch's message has gone", () => {
    const CODEX_ACCOUNT = {
      ...ACCOUNT,
      id: "acct-2",
      provider: "OpenAI",
      harness: "codex",
      harness_label: "Codex",
      label: "OpenAI (Codex)",
    };
    const ASTRA_PICK = {
      identity: { model_id: "gpt-6-astra", effort: "high", fast: false },
      label: "GPT-6 Astra · High",
      option: { ...OPUS, id: "gpt-6-astra", label: "GPT-6 Astra" },
    };

    beforeEach(() => {
      providerState.accounts = [ACCOUNT, CODEX_ACCOUNT];
    });

    it("names the pick without the next mark while the request is out", () => {
      setPendingSwitch("a1", "acct-2", ASTRA_PICK);
      setSwitchSending("a1", true);
      render();
      expect(ROOT().textContent).toContain("GPT-6 Astra · High");
      expect(ROOT().textContent).not.toContain("next");
    });

    it("names the target, not next, while the switch runs, with rows that state it rather than offer it", () => {
      setPendingAccount("a1", "acct-2");
      agentState.agent = chatSnapshotFixture("a1", {
        active_agent: { harness: "claude", account_id: "acct-1" },
        handoff: handoffStateFixture({ phase: "summarizing", target_account_id: "acct-2" }),
      });
      render();
      // No pick: the harness has not said what its default is yet, so the chip names the harness.
      expect(ROOT().textContent).toContain("Codex");
      expect(ROOT().textContent).not.toContain("next");
      click(".model-selector-trigger");
      expect(document.querySelector('[data-menu-row="providers"]')?.textContent).toContain("switching");
      expect(document.querySelector('[data-menu-row="providers"]')?.textContent).not.toContain("next message");
      expect(document.querySelector('[data-menu-row="model"]')?.textContent).toContain("Default model");
      expect(document.querySelector('[data-menu-row="model"] button')).toBeNull();
    });

    it("reads the live choice, with no next mark anywhere, after the switch failed", () => {
      setPendingAccount("a1", "acct-2");
      agentState.agent = chatSnapshotFixture("a1", {
        active_agent: { harness: "claude", account_id: "acct-1" },
        handoff: handoffStateFixture({ phase: "failed", error: "boom", failed_step: "start" }),
      });
      render();
      expect(ROOT().textContent).toContain("Opus");
      expect(ROOT().textContent).not.toContain("next");
      click(".model-selector-trigger");
      click('[data-menu-row="providers"]');
      expect(document.querySelector('[data-menu-part="submenu"]')?.textContent).not.toContain("next");

      // Unmarked, its row is an ordinary one: pressing it begins a switch, not a silent take-back.
      const failedRow = [...document.querySelectorAll('[data-menu-part="submenu"] button')].find((b) =>
        (b.textContent ?? "").includes("OpenAI"),
      );
      if (failedRow === undefined) throw new Error("no row for the failed switch's account");
      failedRow.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      expect(begun).toEqual(["acct-2"]);
      expect(getPendingAccountId("a1")).toBe("acct-2");
    });
  });

  it("drops a failed switch's pick, which the chat keeps for the retry but never applied", () => {
    // The failed switch still carries its pick, for the retry; the agent never took it.
    agentState.agent = chatSnapshotFixture("a1", {
      active_agent: { harness: "claude", account_id: "acct-1" },
      handoff: rebindStateFixture({
        phase: "failed",
        failed_step: "model",
        error: "Unknown model",
        model_pick: { model_id: "opus", effort: "high", fast: false },
      }),
    });
    render();
    expect(ROOT().textContent).toContain("Opus");
    expect(ROOT().textContent).not.toContain("High");
    expect(ROOT().textContent).not.toContain("next");
  });

  it("names a reloaded switch's pick by its id for a harness whose models no catalog holds", () => {
    // codex's option set is per agent, so a pick of one is named by the id it was made under.
    agentState.agent = chatSnapshotFixture("a1", {
      active_agent: { harness: "claude", account_id: "acct-1" },
      handoff: handoffStateFixture({ model_pick: { model_id: "gpt-6-astra", effort: null, fast: false } }),
    });
    render();
    expect(ROOT().textContent).toContain("gpt-6-astra");
  });

  it("leaves the chip on the live choice for a switch that picked no model", () => {
    agentState.agent = chatSnapshotFixture("a1", {
      active_agent: { harness: "claude", account_id: "acct-1" },
      handoff: rebindStateFixture(),
    });
    render();
    expect(ROOT().textContent).toContain("Opus");
    expect(ROOT().textContent).not.toContain("next");
  });

  it("stars the default account and pins another on a press of its star", () => {
    providerState.accounts = [ACCOUNT, { ...ACCOUNT, id: "acct-2", provider: "Google", harness: "antigravity" }];
    providerState.defaultId = "acct-1";
    render();
    click(".model-selector-trigger");
    click('[data-menu-row="providers"]');
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
    click('[data-menu-row="providers"]');
    expect(screenText()).not.toContain("Remove account");
    click('[aria-label="Sign out of Anthropic"]');
    expect(screenText()).toContain("Remove account");

    // Closing and reopening must not leave the confirmation up.
    click(".model-selector-trigger");
    click(".model-selector-trigger");
    click('[data-menu-row="providers"]');
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

  it("states the chat's fast mode on the fast row and picks another from its submenu", () => {
    // The row says which of the three modes the chat is in rather than showing a switch, since
    // auto is neither on nor off; the submenu is where the modes are picked and auto's turn
    // limit lives.
    withFastModel();
    chatSettingsState.settings = {
      ...DEFAULT_CHAT_SETTINGS,
      fast_mode_turn_limit: 3,
      is_fast_mode_notice_shown: true,
    };
    fastModeState.state = { mode: "auto", is_switched: true };
    render();
    click(".model-selector-trigger");
    const row = document.querySelector<HTMLElement>('[data-menu-row="fast"]');
    if (row === null) throw new Error("no fast row");
    expect(row.textContent).toContain("Fast mode");
    expect(row.textContent).toContain("Auto (off now)");
    expect(document.querySelector(".fast-limit-input")).toBeNull();

    click('[data-menu-row="fast"]');
    const submenu = document.querySelector<HTMLElement>('[data-menu-part="submenu"]');
    if (submenu === null) throw new Error("no fast-mode submenu");
    expect(submenu.querySelector('[data-fast-mode="auto"]')?.getAttribute("aria-checked")).toBe("true");
    expect(submenu.querySelector('[data-fast-mode="on"]')?.getAttribute("aria-checked")).toBe("false");
    expect(submenu.querySelector('[data-fast-mode="auto"]')?.textContent).toContain(
      "Fast for the first 3 turns, then standard",
    );
    expect(submenu.querySelector('[data-fast-mode="auto"]')?.textContent).toContain("(off now)");

    click('[data-fast-mode="auto"]');
    expect(fastModeChoices).toEqual([]);
    click('[data-fast-mode="on"]');
    expect(fastModeChoices).toEqual([["a1", "on"]]);
    expect(document.querySelector('[data-menu-part="submenu"]')).not.toBeNull();
    expect(document.querySelector('[data-menu-part="menu"]')).not.toBeNull();
  });

  it("offers auto's turn limit only under auto, and writes a changed one to the settings", () => {
    withFastModel();
    render();
    click(".model-selector-trigger");
    click('[data-menu-row="fast"]');
    const limit = document.querySelector<HTMLInputElement>(".fast-limit-input");
    if (limit === null) throw new Error("no turn-limit field under Auto");
    expect(limit.value).toBe("2");

    limit.value = "3";
    limit.dispatchEvent(new Event("input", { bubbles: true }));
    render();
    // The field keeps what is being typed across the redraws every keystroke causes.
    expect(limit.value).toBe("3");
    limit.dispatchEvent(new Event("change", { bubbles: true }));
    expect(settingsWrites).toEqual([{ ...DEFAULT_CHAT_SETTINGS, fast_mode_turn_limit: 3 }]);

    // An emptied field or a zero is not a limit.
    limit.value = "";
    limit.dispatchEvent(new Event("change", { bubbles: true }));
    limit.value = "0";
    limit.dispatchEvent(new Event("change", { bubbles: true }));
    expect(settingsWrites).toHaveLength(1);

    // The limit belongs to auto: no other mode runs to one.
    fastModeState.state = { mode: "on", is_switched: false };
    render();
    expect(document.querySelector(".fast-limit-input")).toBeNull();
  });

  it("holds the fast submenu open while a limit is half typed, and lets it go once there is none", () => {
    vi.useFakeTimers();
    withFastModel();
    render();
    click(".model-selector-trigger");
    click('[data-menu-row="fast"]');
    const limit = document.querySelector<HTMLInputElement>(".fast-limit-input");
    if (limit === null) throw new Error("no turn-limit field under Auto");
    limit.value = "12";
    limit.dispatchEvent(new Event("input", { bubbles: true }));
    render();

    leaveSubmenu();
    expect(document.querySelector('[data-menu-part="submenu"]')).not.toBeNull();

    // Filed, so there is nothing left to lose and the drift closes it again.
    limit.dispatchEvent(new Event("change", { bubbles: true }));
    render();
    leaveSubmenu();
    expect(document.querySelector('[data-menu-part="submenu"]')).toBeNull();

    // A pick that takes the field away abandons the half-typed number with it, without relying
    // on the field's own blur.
    click('[data-menu-row="fast"]');
    const retyped = document.querySelector<HTMLInputElement>(".fast-limit-input");
    if (retyped === null) throw new Error("no turn-limit field under Auto");
    retyped.value = "12";
    retyped.dispatchEvent(new Event("input", { bubbles: true }));
    render();
    click('[data-fast-mode="on"]');
    fastModeState.state = { mode: "on", is_switched: false };
    render();
    expect(document.querySelector(".fast-limit-input")).toBeNull();
    leaveSubmenu();
    expect(document.querySelector('[data-menu-part="submenu"]')).toBeNull();
  });

  it("names the mode new chats start in, and sets it to any mode whatever the chat's own", () => {
    withFastModel();
    fastModeState.state = { mode: "on", is_switched: false };
    render();
    click(".model-selector-trigger");
    click('[data-menu-row="fast"]');
    const row = document.querySelector<HTMLElement>(".fast-mode-default");
    if (row === null) throw new Error("no new-chats row");
    expect(row.textContent).toContain("New chats start with");
    expect(row.querySelector('[role="radiogroup"]')?.getAttribute("aria-label")).toBe("Fast mode for new chats");
    const options = [...row.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
    expect(options.map((option) => option.getAttribute("data-fast-mode-default"))).toEqual(["off", "auto", "on"]);
    expect(options.map((option) => option.textContent)).toEqual(["Off", "Auto", "On"]);
    // The workspace's setting is the one lit, not the chat's own mode.
    expect(options.map((option) => option.getAttribute("aria-checked"))).toEqual(["false", "true", "false"]);
    expect(options.every((option) => !option.disabled)).toBe(true);

    // Away from the chat's mode...
    click('[data-fast-mode-default="off"]');
    expect(settingsWrites).toEqual([{ ...DEFAULT_CHAT_SETTINGS, fast_mode_default: "off" }]);
    chatSettingsState.settings = { ...DEFAULT_CHAT_SETTINGS, fast_mode_default: "off" };
    render();
    expect(
      document.querySelector('[data-fast-mode-default][aria-checked="true"]')?.getAttribute("data-fast-mode-default"),
    ).toBe("off");
    // ...back again, and to the chat's mode.
    click('[data-fast-mode-default="auto"]');
    click('[data-fast-mode-default="on"]');
    expect(settingsWrites.slice(1)).toEqual([
      { ...DEFAULT_CHAT_SETTINGS, fast_mode_default: "auto" },
      { ...DEFAULT_CHAT_SETTINGS, fast_mode_default: "on" },
    ]);
    // The mode already set is not a change.
    click('[data-fast-mode-default="off"]');
    expect(settingsWrites).toHaveLength(3);
    // None of it touches the chat's own mode.
    expect(fastModeChoices).toEqual([]);
  });

  it("leaves the new-chats modes unlit and unpressable until the settings load", () => {
    withFastModel();
    chatSettingsState.settings = null;
    render();
    click(".model-selector-trigger");
    click('[data-menu-row="fast"]');
    const options = [...document.querySelectorAll<HTMLButtonElement>("[data-fast-mode-default]")];
    expect(options).toHaveLength(3);
    expect(options.every((option) => option.disabled)).toBe(true);
    expect(options.every((option) => option.getAttribute("aria-checked") === "false")).toBe(true);
    click('[data-fast-mode-default="on"]');
    expect(settingsWrites).toEqual([]);
    expect(chatSettingsState.loads).toBeGreaterThan(0);

    chatSettingsState.settings = DEFAULT_CHAT_SETTINGS;
    render();
    expect(document.querySelector<HTMLButtonElement>('[data-fast-mode-default="on"]')?.disabled).toBe(false);
  });

  it("asks for the chat's fast mode and shows the row unresolved until it is known", () => {
    withFastModel();
    fastModeState.state = null;
    render();
    click(".model-selector-trigger");
    const row = document.querySelector<HTMLElement>('[data-menu-row="fast"]');
    if (row === null) throw new Error("no fast row");
    expect(row.textContent).toContain("...");
    expect(fastModeLoads).toContain("a1");
  });

  it("offers Auto-compact after the fast row for a harness that can be compacted, and not for one that cannot", () => {
    withFastModel();
    render();
    click(".model-selector-trigger");
    expect(document.querySelector('[data-menu-row="autocompact"]')).toBeNull();

    catalogState.catalog = { ...(catalogState.catalog as Record<string, unknown>), supports_compaction: true };
    render();
    const row = document.querySelector<HTMLElement>('[data-menu-row="autocompact"]');
    if (row === null) throw new Error("no auto-compact row");
    expect(row.textContent).toContain("Auto-compact");
    expect(row.textContent).toContain("On");
    const rowKeys = [...document.querySelectorAll("[data-menu-row]")].map((each) =>
      each.getAttribute("data-menu-row"),
    );
    expect(rowKeys.indexOf("autocompact")).toBe(rowKeys.indexOf("fast") + 1);

    // It belongs to the harness, not the model: a model with no fast mode still has it.
    catalogState.catalog = catalogOf({ supports_compaction: true });
    settingsState.choice = { identity: { model_id: "opus", effort: null, fast: false }, matched: OPUS, pending: null };
    render();
    expect(document.querySelector('[data-menu-row="fast"]')).toBeNull();
    expect(document.querySelector('[data-menu-row="autocompact"]')).not.toBeNull();
  });

  it("turns auto-compact off for the chat from its submenu, which stays up", () => {
    catalogState.catalog = catalogOf({ supports_compaction: true });
    render();
    click(".model-selector-trigger");
    click('[data-menu-row="autocompact"]');
    const submenu = document.querySelector<HTMLElement>('[data-menu-part="submenu"]');
    if (submenu === null) throw new Error("no auto-compact submenu");
    expect(submenu.querySelector('[role="radiogroup"]')?.getAttribute("aria-label")).toBe("Auto-compact");
    expect(submenu.querySelector('[data-autocompact="on"]')?.getAttribute("aria-checked")).toBe("true");
    expect(submenu.querySelector('[data-autocompact="off"]')?.getAttribute("aria-checked")).toBe("false");
    expect(submenu.querySelector(".autocompact-help")?.textContent).toBe(
      "Saves ~50% by compacting right before the cache expires.",
    );

    click('[data-autocompact="on"]');
    expect(autocompactWrites).toEqual([]);
    click('[data-autocompact="off"]');
    expect(autocompactWrites).toEqual([["a1", { is_enabled: false }]]);
    expect(document.querySelector('[data-menu-part="submenu"]')).not.toBeNull();
    expect(document.querySelector('[data-menu-part="menu"]')).not.toBeNull();
  });

  it("names what new chats start with for auto-compact, and sets it either way whatever the chat's own", () => {
    catalogState.catalog = catalogOf({ supports_compaction: true });
    autocompactState.state = { is_enabled: true };
    render();
    click(".model-selector-trigger");
    click('[data-menu-row="autocompact"]');
    const row = document.querySelector<HTMLElement>(".autocompact-default");
    if (row === null) throw new Error("no new-chats row");
    expect(row.textContent).toContain("New chats start with");
    expect(row.querySelector('[role="radiogroup"]')?.getAttribute("aria-label")).toBe("Auto-compact for new chats");
    const options = [...row.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
    expect(options.map((option) => option.getAttribute("data-autocompact-default"))).toEqual(["on", "off"]);
    expect(options.map((option) => option.textContent)).toEqual(["On", "Off"]);
    expect(options.map((option) => option.getAttribute("aria-checked"))).toEqual(["true", "false"]);

    // Off for new chats while this chat stays on...
    click('[data-autocompact-default="off"]');
    expect(settingsWrites).toEqual([{ ...DEFAULT_CHAT_SETTINGS, autocompact_default: false }]);
    chatSettingsState.settings = { ...DEFAULT_CHAT_SETTINGS, autocompact_default: false };
    render();
    expect(document.querySelector('[data-autocompact-default="off"]')?.getAttribute("aria-checked")).toBe("true");
    expect(document.querySelector('[data-autocompact-default="on"]')?.getAttribute("aria-checked")).toBe("false");
    expect(document.querySelector('[data-autocompact="on"]')?.getAttribute("aria-checked")).toBe("true");
    // ...and back to on, with the chat turned off.
    autocompactState.state = { is_enabled: false };
    render();
    click('[data-autocompact-default="on"]');
    expect(settingsWrites[1]).toEqual({ ...DEFAULT_CHAT_SETTINGS, autocompact_default: true });
    // The setting already in place is not a change, and none of it touches the chat's own.
    click('[data-autocompact-default="off"]');
    expect(settingsWrites).toHaveLength(2);
    expect(autocompactWrites).toEqual([]);
  });

  it("asks for the chat's auto-compact setting and shows the row unresolved until it is known", () => {
    catalogState.catalog = catalogOf({ supports_compaction: true });
    autocompactState.state = null;
    render();
    click(".model-selector-trigger");
    const row = document.querySelector<HTMLElement>('[data-menu-row="autocompact"]');
    if (row === null) throw new Error("no auto-compact row");
    expect(row.textContent).toContain("...");
    expect(autocompactLoads).toContain("a1");

    // The submenu reads the workspace default until the chat's own setting arrives.
    chatSettingsState.settings = { ...DEFAULT_CHAT_SETTINGS, autocompact_default: false };
    click('[data-menu-row="autocompact"]');
    expect(document.querySelector('[data-autocompact="off"]')?.getAttribute("aria-checked")).toBe("true");
  });

  it("states auto-compact on a read-only harness without a submenu to open", () => {
    catalogState.catalog = catalogOf({ switch_mode: "read_only", supports_compaction: true });
    autocompactState.state = { is_enabled: false };
    render();
    click(".model-selector-trigger");
    const row = document.querySelector<HTMLElement>('[data-menu-row="autocompact"]');
    if (row === null) throw new Error("no auto-compact row");
    expect(row.textContent).toContain("Off");
    expect(row.querySelector("svg")).toBeNull();
    click('[data-menu-row="autocompact"]');
    expect(document.querySelector('[data-menu-part="submenu"]')).toBeNull();
  });

  it("gives a read-only harness no model list to open", () => {
    // agy's `/model` is an interactive TUI with no scriptable form. A chevron on that row
    // would be a promise the card cannot keep.
    catalogState.catalog = catalogOf({ switch_mode: "read_only" });
    render();
    click(".model-selector-trigger");
    expect(document.querySelector('[data-menu-row="model"]')?.querySelector("svg")).toBeNull();
    click('[data-menu-row="model"]');
    expect(document.querySelector('[data-menu-part="submenu"]')).toBeNull();
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

describe("the phone layout's card", () => {
  const EFFORTS = [
    { level: "low", in_picker: true },
    { level: "medium", in_picker: true },
    { level: "high", in_picker: true },
    { level: "ultra", in_picker: false },
  ];
  const toggles: number[] = [];

  function renderPhone(isSourceViewOn = false): void {
    m.render(
      ROOT(),
      m(ModelProviderMenu as never, {
        chatId: "a1",
        isCompact: true,
        sourceView: { on: isSourceViewOn, onToggle: () => toggles.push(1) },
      }),
    );
  }

  function tap(selector: string): void {
    const node = document.querySelector<HTMLElement>(selector);
    if (node === null) throw new Error(`no ${selector} on screen`);
    node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    renderPhone();
  }

  beforeEach(() => {
    toggles.length = 0;
    const model = { ...OPUS, efforts: EFFORTS };
    catalogState.catalog = catalogOf({ options: [model] });
    settingsState.choice = {
      identity: { model_id: "opus", effort: "medium", fast: false },
      matched: model,
      pending: null,
    };
  });

  it("opens from a settings button instead of the chip", () => {
    renderPhone();
    expect(ROOT().querySelector(".model-selector-trigger")).toBeNull();
    tap("[data-composer-settings]");
    expect(document.querySelector(".model-provider-menu--compact")).not.toBeNull();
    expect(screenText()).toContain("Provider");
    expect(screenText()).toContain("Stop agent");
  });

  it("keeps the settings button for a chat whose account was signed out, with a Provider row to move it on", () => {
    agentState.agent = chatSnapshotFixture("a1", { active_agent: { harness: "claude", account_id: "acct-gone" } });
    renderPhone();
    tap("[data-composer-settings]");
    const providerRow = document.querySelector('[data-menu-row="providers"]')?.textContent ?? "";
    expect(providerRow).toContain("No account");
    expect(providerRow).toContain("Pick one to move this chat to it");
    expect(screenText()).not.toContain("Not signed in");
  });

  it("offers the picker's effort levels as segments, and a press sets the chat's effort at once", () => {
    renderPhone();
    tap("[data-composer-settings]");
    expect(document.querySelector('input[type="range"]')).toBeNull();
    const segments = [...document.querySelectorAll<HTMLElement>("[data-effort-level]")];
    expect(segments.map((segment) => segment.textContent)).toEqual(["Low", "Medium", "High"]);
    expect(segments.map((segment) => segment.getAttribute("aria-checked"))).toEqual(["false", "true", "false"]);

    tap('[data-effort-level="high"]');
    expect(picks).toHaveLength(1);
    expect((picks[0] as unknown[])[1]).toEqual({ model_id: "opus", effort: "high", fast: false });
    // The chat's own level again is not a change.
    tap('[data-effort-level="medium"]');
    expect(picks).toHaveLength(1);
  });

  it("slides a submenu in over the card's rows, and its back row slides it out", () => {
    // Browsers reflect `inert` as a boolean property, which mithril assigns rather than setting the attribute;
    // jsdom has none.
    Object.defineProperty(HTMLElement.prototype, "inert", {
      configurable: true,
      get(this: HTMLElement) {
        return this.hasAttribute("inert");
      },
      set(this: HTMLElement, value: unknown) {
        this.toggleAttribute("inert", Boolean(value));
      },
    });
    try {
      renderPhone();
      tap("[data-composer-settings]");
      const track = (): string | null =>
        document.querySelector("[data-menu-track]")?.getAttribute("data-menu-track") ?? null;
      const inertPanes = (): boolean[] =>
        [...document.querySelectorAll<HTMLElement>(".sliding-menu-track > div")].map((pane) => pane.inert);
      expect(track()).toBe("menu");
      expect(inertPanes()).toEqual([false, true]);

      tap('[data-menu-row="providers"]');
      expect(track()).toBe("submenu");
      expect(inertPanes()).toEqual([true, false]);
      // A slide, not a flyout: the submenu is inside the card, and there is no second box beside it.
      expect(document.querySelector('[data-menu-part="submenu"]')).toBeNull();
      expect(screenText()).toContain("+ Add a provider");

      tap("[data-menu-track-back]");
      expect(track()).toBe("menu");
      expect(inertPanes()).toEqual([false, true]);
      expect(screenText()).not.toContain("+ Add a provider");
    } finally {
      delete (HTMLElement.prototype as { inert?: boolean }).inert;
    }
  });

  it("sets what new chats start with from a submenu slid in over the card", () => {
    catalogState.catalog = { ...(catalogState.catalog as Record<string, unknown>), supports_compaction: true };
    renderPhone();
    tap("[data-composer-settings]");
    tap('[data-menu-row="autocompact"]');
    expect(document.querySelector('[data-menu-part="submenu"]')).toBeNull();
    expect(document.querySelector('[data-autocompact-default="on"]')?.getAttribute("aria-checked")).toBe("true");
    tap('[data-autocompact-default="off"]');
    expect(settingsWrites).toEqual([{ ...DEFAULT_CHAT_SETTINGS, autocompact_default: false }]);
  });

  it("carries the Source view switch as a row, whose press turns the card over and closes the menu", () => {
    renderPhone();
    tap("[data-composer-settings]");
    const row = document.querySelector<HTMLElement>('[data-menu-row="source-view"] [role="switch"]');
    expect(row?.getAttribute("aria-checked")).toBe("false");
    tap('[data-menu-row="source-view"] [role="switch"]');
    expect(toggles).toHaveLength(1);
    expect(document.querySelector(".model-provider-menu")).toBeNull();
  });

  it("keeps the settings button for a chat with no account and no model, with Source view and Stop agent", () => {
    agentState.agent = chatSnapshotFixture("a1", { active_agent: { harness: "claude", account_id: "" } });
    settingsState.choice = null;
    // Whether or not some other provider is signed in.
    for (const accounts of [[], [ACCOUNT]]) {
      providerState.accounts = accounts;
      renderPhone();
      tap("[data-composer-settings]");
      expect(screenText()).toContain("No account");
      expect(document.querySelector('[data-menu-row="source-view"]')).not.toBeNull();
      expect(screenText()).toContain("Stop agent");
      tap("[data-composer-settings]");
    }
  });

  it("leaves the desktop card as it was: the chip, the slider, and no Source view row", () => {
    render();
    click(".model-selector-trigger");
    expect(document.querySelector('input[type="range"]')).not.toBeNull();
    expect(document.querySelector("[data-effort-level]")).toBeNull();
    expect(document.querySelector("[data-menu-track]")).toBeNull();
    expect(document.querySelector('[data-menu-row="source-view"]')).toBeNull();
  });
});
