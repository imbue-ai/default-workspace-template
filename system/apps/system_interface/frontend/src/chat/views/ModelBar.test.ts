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
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

const agentState: { agent: unknown } = { agent: null };
vi.mock("../models/AgentManager", () => ({
  getAgentById: () => agentState.agent,
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

const providerState: { accounts: unknown[] } = { accounts: [] };
vi.mock("../../models/Providers", () => ({
  getAccounts: () => providerState.accounts,
  accountForAgent: (id?: string) => providerState.accounts.find((a) => (a as { id: string }).id === id) ?? null,
  openProviderChooser: () => undefined,
  deleteAccount: () => Promise.resolve(),
  renameAccount: () => Promise.resolve(),
}));

// The card's "start a chat on that provider" ask goes to the shell through chat/shell.ts.
const started: string[] = [];
vi.mock("../shell", () => ({
  startChatOnAccount: (accountId: string) => started.push(accountId),
  openSubagentTab: vi.fn(),
}));

import m from "mithril";

import { ModelBar } from "./ModelBar";

const ROOT = () => document.getElementById("root") as HTMLElement;

function render(): void {
  m.render(ROOT(), m(ModelBar as never, { agentId: "a1" }));
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

beforeEach(() => {
  document.body.innerHTML = '<div id="root"></div>';
  picks.length = 0;
  started.length = 0;
  agentState.agent = { id: "a1", harness: "claude", labels: { account: "acct-1" } };
  catalogState.catalog = catalogOf();
  settingsState.choice = { identity: { model_id: "opus", effort: null, fast: false }, matched: OPUS, pending: null };
  providerState.accounts = [ACCOUNT];
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

  it("locks every provider that is not this chat's, and says how to reach it", () => {
    // A chat binds to its account when it is CREATED and nothing rebinds it, so there is no
    // state in which switching would work. The row states that rather than doing something
    // else by surprise -- the tooltip is the whole affordance.
    providerState.accounts = [
      ACCOUNT,
      { ...ACCOUNT, id: "acct-2", provider: "Google", harness: "antigravity", harness_label: "Antigravity CLI" },
    ];
    render();
    click(".model-selector-trigger");
    click('[data-card-row="providers"]');
    const rows = [...document.querySelectorAll("button")].filter((b) => (b.textContent ?? "").includes("Google"));
    expect(rows).toHaveLength(1);
    rows[0].dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(started).toEqual([]);
    // The hint itself is a hover-intent bubble on <body> (hoverTooltip.ts), so it is not in
    // the tree at render time -- what this pins is that pressing a locked row does NOTHING.
    expect(document.querySelector('[data-model-popover="flyout"]')).not.toBeNull();
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
