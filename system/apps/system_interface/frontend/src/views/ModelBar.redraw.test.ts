// @vitest-environment jsdom
/**
 * The card must react to the FIRST click, on its own.
 *
 * This is the one thing `ModelBar.test.ts` cannot catch: its `click()` helper re-renders by
 * hand afterwards, which supplies exactly the redraw whose absence was the bug. The card and
 * its flyouts are drawn through `Portal` -> `m.render`, mithril's manual API, which does not
 * wire auto-redraw into event handlers. Every handler inside them set state and nothing
 * re-rendered: a row click opened no flyout, a trash click armed no "Remove?", and the click
 * after that landed outside and tore the whole thing down.
 *
 * So this file MOUNTS the component (auto-redraw on, like the real app) and never renders by
 * hand. If the portal stops driving redraws again, these fail.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

const agentState: { agent: unknown } = { agent: null };
vi.mock("../models/AgentManager", () => ({ getAgentById: () => agentState.agent }));

const catalogState: { catalog: unknown } = { catalog: null };
vi.mock("../models/HarnessCatalog", () => ({
  ensureHarnessCatalogs: () => undefined,
  getHarnessCatalog: () => catalogState.catalog,
}));

const settingsState: { choice: unknown } = { choice: null };
vi.mock("../models/ModelSettings", () => ({
  effectiveChoice: () => settingsState.choice,
  changedAxes: () => ["model"],
  setModelChoice: () => undefined,
}));

const providerState: { accounts: unknown[] } = { accounts: [] };
const chooserOpens: number[] = [];
const deleted: string[] = [];
const renamed: [string, string][] = [];
vi.mock("../models/Providers", () => ({
  getAccounts: () => providerState.accounts,
  accountForAgent: (id?: string) => providerState.accounts.find((a) => (a as { id: string }).id === id) ?? null,
  openProviderChooser: () => chooserOpens.push(1),
  deleteAccount: (id: string) => {
    deleted.push(id);
    return Promise.resolve();
  },
  renameAccount: (id: string, name: string) => {
    renamed.push([id, name]);
    return Promise.resolve();
  },
}));

vi.mock("./DockviewWorkspace", () => ({ startChatOnAccount: () => undefined }));

import m from "mithril";

import { ModelBar } from "./ModelBar";

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

/** Let mithril's frame-batched redraw land. */
async function settle(): Promise<void> {
  // Waits on real animation FRAMES, not bare macrotasks. Mithril batches its redraw onto
  // `requestAnimationFrame`, and jsdom schedules that ~16ms out -- so three `setTimeout(0)`
  // ticks resolve long before the redraw they are supposed to be waiting for, and every
  // assertion here reads a DOM that has not been updated yet.
  for (let i = 0; i < 3; i += 1) {
    await new Promise((resolve) => {
      requestAnimationFrame(() => resolve(undefined));
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

/** Press something. No hand-rendering afterwards -- that is the whole point. */
async function press(selector: string): Promise<void> {
  const node = document.querySelector<HTMLElement>(selector);
  if (node === null) throw new Error(`no ${selector} on screen`);
  node.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  await settle();
}

beforeEach(() => {
  // Unmount the previous root before wiping the DOM, so its `onremove` runs: the component
  // takes a document-level mousedown listener and the portal leaves a host on <body>, and
  // neither is cleaned up by replacing innerHTML out from under it.
  const previous = document.getElementById("root");
  if (previous !== null) m.mount(previous, null);
  document.body.innerHTML = '<div id="root"></div>';
  chooserOpens.length = 0;
  deleted.length = 0;
  renamed.length = 0;
  agentState.agent = { id: "a1", harness: "claude", labels: { account: "acct-1" } };
  catalogState.catalog = {
    switch_mode: "eager_then_reconcile",
    picker_mode: "list",
    options: [OPUS],
    native_atomic_shoulder_tap_possible: true,
    popups: [],
  };
  settingsState.choice = { identity: { model_id: "opus", effort: null, fast: false }, matched: OPUS, pending: null };
  providerState.accounts = [ACCOUNT];
  // MOUNTED, not rendered: this is what gives handlers in the main tree their auto-redraw,
  // and what the portal has to reproduce for the handlers inside it.
  m.mount(document.getElementById("root") as HTMLElement, {
    view: () => m(ModelBar as never, { agentId: "a1" }),
  });
});

describe("the card without a hand-cranked redraw", () => {
  it("opens a flyout on the first press of a row", async () => {
    await press(".model-selector-trigger");
    expect(document.querySelector('[data-model-popover="card"]')).not.toBeNull();

    await press('[data-card-row="providers"]');
    expect(document.querySelector('[data-model-popover="flyout"]')).not.toBeNull();
  });

  it("opens the removal dialog on a trash press instead of closing the picker", async () => {
    await press(".model-selector-trigger");
    await press('[data-card-row="providers"]');
    await press('[aria-label="Sign out of Anthropic"]');

    // The bug: mousedown read as outside, the popover went away, and the click never landed.
    expect(document.querySelector('[data-model-popover="flyout"]')).not.toBeNull();
    expect(document.body.textContent).toContain("Remove account");
    expect(deleted).toEqual([]);

    // Only the dialog's own red button acts.
    await press(".destroy-dialog-btn-destroy");
    expect(deleted).toEqual(["acct-1"]);
  });

  it("turns a row into a rename field and files what was typed on blur", async () => {
    await press(".model-selector-trigger");
    await press('[data-card-row="providers"]');
    await press('[aria-label="Rename Anthropic"]');

    const field = document.querySelector<HTMLInputElement>('input[aria-label="Rename Anthropic"]');
    if (field === null) throw new Error("no rename field on screen");
    // EMPTY, with the row's current name as the placeholder. Seeding it with what the row was
    // showing meant seeding from `provider`, which carries the disambiguating number -- so
    // renaming an unnamed duplicate and pressing Enter filed "Anthropic 2" as a real chosen
    // name, which then outlives the account it was counting against.
    expect(field.value).toBe("");
    expect(field.placeholder).toBe("Anthropic");
    // Every other control stands down while the field is up, so nothing destructive sits
    // under a pointer that came to click into text.
    expect(document.querySelector('[aria-label="Sign out of Anthropic"]')).toBeNull();

    field.value = "Work";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
    await settle();
    expect(renamed).toEqual([["acct-1", "Work"]]);
  });

  it("does not read retyping an already-chosen name as a request to clear it", async () => {
    // Found live: a renamed row SHOWS its chosen name, so comparing what was typed against
    // the displayed name read "Work" over "Work" as "reset to the provider" and wiped it.
    providerState.accounts = [{ ...ACCOUNT, provider: "Work", name: "Work" }];
    await press(".model-selector-trigger");
    await press('[data-card-row="providers"]');
    await press('[aria-label="Rename Work"]');

    const field = document.querySelector<HTMLInputElement>('input[aria-label="Rename Work"]');
    if (field === null) throw new Error("no rename field on screen");
    expect(field.value).toBe("Work");
    field.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
    await settle();
    expect(renamed).toEqual([]);
  });

  it("clears the name when the field is emptied, which is the only way back", async () => {
    providerState.accounts = [{ ...ACCOUNT, provider: "Work", name: "Work" }];
    await press(".model-selector-trigger");
    await press('[data-card-row="providers"]');
    await press('[aria-label="Rename Work"]');

    const field = document.querySelector<HTMLInputElement>('input[aria-label="Rename Work"]');
    if (field === null) throw new Error("no rename field on screen");
    field.value = "";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
    await settle();
    expect(renamed).toEqual([["acct-1", ""]]);
  });

  it("discards a rename on Escape, and does not re-file it on the blur that follows", async () => {
    await press(".model-selector-trigger");
    await press('[data-card-row="providers"]');
    await press('[aria-label="Rename Anthropic"]');

    const field = document.querySelector<HTMLInputElement>('input[aria-label="Rename Anthropic"]');
    if (field === null) throw new Error("no rename field on screen");
    field.value = "Work";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();
    // Removing the field fires a native blur; without the guard that blur commits the very
    // name Escape just threw away.
    field.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
    await settle();
    expect(renamed).toEqual([]);
  });

  it("cancels the removal dialog without deleting anything", async () => {
    await press(".model-selector-trigger");
    await press('[data-card-row="providers"]');
    await press('[aria-label="Sign out of Anthropic"]');
    expect(document.body.textContent).toContain("Remove account");

    await press(".destroy-dialog-btn-cancel");
    expect(document.body.textContent).not.toContain("Remove account");
    expect(deleted).toEqual([]);
    // Backing out of the dialog must not have taken the flyout down with it.
    expect(document.querySelector('[data-model-popover="flyout"]')).not.toBeNull();
  });

  it("keeps the removal dialog open while the pointer wanders the rest of the submenu", async () => {
    await press(".model-selector-trigger");
    await press('[data-card-row="providers"]');
    await press('[aria-label="Sign out of Anthropic"]');
    document
      .querySelector('[data-model-popover="flyout"]')
      ?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    await settle();
    expect(document.body.textContent).toContain("Remove account");
  });

  it("opens the chooser from + Add a provider, and takes the picker down with it", async () => {
    await press(".model-selector-trigger");
    await press('[data-card-row="providers"]');
    const add = [...document.querySelectorAll("button")].find((b) => (b.textContent ?? "").includes("Add a provider"));
    if (add === undefined) throw new Error("no add-provider row");
    add.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    add.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();

    expect(chooserOpens).toEqual([1]);
    expect(document.querySelector('[data-model-popover="card"]')).toBeNull();
    expect(document.querySelector('[data-model-popover="flyout"]')).toBeNull();
  });

  it("ignores a press on a locked provider, without closing anything", async () => {
    providerState.accounts = [
      ACCOUNT,
      { ...ACCOUNT, id: "acct-2", provider: "Google", harness: "antigravity", harness_label: "Antigravity CLI" },
    ];
    await press(".model-selector-trigger");
    await press('[data-card-row="providers"]');
    const locked = [...document.querySelectorAll("button")].find((b) => (b.textContent ?? "").includes("Google"));
    if (locked === undefined) throw new Error("no locked row");
    locked.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    locked.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();

    expect(document.querySelector('[data-model-popover="flyout"]')).not.toBeNull();
  });

  it("closes the whole stack on a click outside, and only on a click", async () => {
    await press(".model-selector-trigger");
    await press('[data-card-row="providers"]');

    // A pointer merely leaving is not a dismissal.
    document
      .querySelector('[data-model-popover="card"]')
      ?.dispatchEvent(new MouseEvent("mouseleave", { bubbles: true }));
    await settle();
    expect(document.querySelector('[data-model-popover="card"]')).not.toBeNull();

    document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    await settle();
    expect(document.querySelector('[data-model-popover="card"]')).toBeNull();
    expect(document.querySelector('[data-model-popover="flyout"]')).toBeNull();
  });
});
