import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  accounts: [] as { id: string; lane: string; provider: string; harness_label: string }[],
  // The account a new chat starts on: the pinned one, else the one the account list would pick.
  selectedAccountId: null as string | null,
  setDefaultAccount: vi.fn((_accountId: string, _isDefault: boolean) => Promise.resolve()),
  loadAccounts: vi.fn(() => Promise.resolve()),
  isPending: true,
  finish: vi.fn(() => Promise.resolve()),
  open: vi.fn(),
  reportOnScreen: vi.fn(),
}));

vi.mock("../shell", () => ({ reportProviderChooserOnScreenOnce: mocks.reportOnScreen }));
vi.mock("../models/Providers", () => ({
  areLanesLoaded: () => true,
  closeProviderChooser: vi.fn(),
  finishFirstRunProviderStep: mocks.finish,
  getAccounts: () => mocks.accounts,
  getLanes: () => [
    { id: "anthropic", provider_name: "Anthropic", subtitle: "long", methods: [] },
    { id: "made-up", provider_name: "Made Up", subtitle: "Its own line", methods: [] },
  ],
  getSelectedAccount: () => mocks.accounts.find((account) => account.id === mocks.selectedAccountId) ?? null,
  loadAccounts: mocks.loadAccounts,
  setDefaultAccount: mocks.setDefaultAccount,
  isFirstRunProviderStepPending: () => mocks.isPending,
  isProviderChooserOpen: () => false,
  loadLanesAndAccountsWithRetry: () => Promise.resolve(),
  openProviderChooser: mocks.open,
}));
vi.mock("./ProviderChooserModal", () => ({ ProviderChooserModal: { view: () => null } }));

import {
  FIRST_RUN_ADD_ANOTHER_HEADING,
  FIRST_RUN_CONNECTED_HEADING,
  FIRST_RUN_DEFAULT_TAG,
  FIRST_RUN_MAKE_DEFAULT_HINT,
  FIRST_RUN_PROVIDERS_TITLE,
  FirstRunProvidersPage,
} from "./FirstRunProvidersPage";

const ANTHROPIC = { id: "acct-1", lane: "anthropic", provider: "Anthropic", harness_label: "Claude Code" };
const OPENAI = { id: "acct-2", lane: "openai", provider: "OpenAI", harness_label: "Codex" };

type Node = m.Vnode<Record<string, unknown>>;

function nodes(tree: unknown): Node[] {
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  if (tree === null || typeof tree !== "object") return [];
  const node = tree as Node;
  return [node, ...nodes(node.children)];
}

function text(tree: unknown): string {
  if (Array.isArray(tree)) return tree.map(text).join("");
  if (typeof tree === "string") return tree;
  if (tree === null || typeof tree !== "object") return "";
  const node = tree as Node & { text?: unknown };
  return (typeof node.text === "string" ? node.text : "") + text(node.children);
}

function byId(tree: unknown, id: string): Node {
  const found = nodes(tree).find((node) => node.attrs?.id === id);
  if (found === undefined) throw new Error(`no #${id}`);
  return found;
}

function accountRow(tree: unknown, accountId: string): Node {
  const found = nodes(tree).find((node) => node.attrs?.["data-first-run-account"] === accountId);
  if (found === undefined) throw new Error(`no row for account ${accountId}`);
  return found;
}

function render(shell = { firstRunProviderStep: vi.fn() }) {
  const page = FirstRunProvidersPage();
  const view = () => page.view!({ attrs: { shell } } as never);
  return { view, shell };
}

describe("FirstRunProvidersPage", () => {
  afterEach(() => {
    mocks.accounts = [];
    mocks.selectedAccountId = null;
    mocks.setDefaultAccount.mockReset();
    mocks.setDefaultAccount.mockImplementation(() => Promise.resolve());
    mocks.loadAccounts.mockClear();
    mocks.isPending = true;
    mocks.finish.mockClear();
    mocks.open.mockClear();
    vi.restoreAllMocks();
  });

  it("offers every lane as a tile and holds Next until a provider is connected", () => {
    const tree = render().view();

    expect(text(tree)).toContain(FIRST_RUN_PROVIDERS_TITLE);
    expect(text(tree)).not.toContain(FIRST_RUN_CONNECTED_HEADING);
    const tiles = nodes(tree).filter((node) => node.attrs?.["data-first-run-lane"] !== undefined);
    expect(tiles.map((tile) => tile.attrs["data-first-run-lane"])).toEqual(["anthropic", "made-up"]);
    // A lane the page has no short line for says its own.
    expect(text(tiles[0])).toContain("Pro, Max or Ultra");
    expect(text(tiles[1])).toContain("Its own line");
    expect(byId(tree, "first-run-next").attrs.disabled).toBe(true);
    expect(byId(tree, "first-run-skip").attrs.disabled).toBe(false);
  });

  it("tells the chrome the provider ask is on screen once its tiles are drawn", () => {
    const page = FirstRunProvidersPage();

    (page.oncreate as () => void)();

    expect(mocks.reportOnScreen).toHaveBeenCalledWith(true);
  });

  it("opens the chooser on the tile's lane", () => {
    const tree = render().view();
    const tile = nodes(tree).find((node) => node.attrs?.["data-first-run-lane"] === "anthropic")!;

    (tile.attrs.onclick as () => void)();

    expect(mocks.open).toHaveBeenCalledWith({ laneId: "anthropic" });
  });

  it("lists a connected account and lets Next leave", () => {
    mocks.accounts = [ANTHROPIC];
    mocks.selectedAccountId = ANTHROPIC.id;
    const tree = render().view();

    expect(text(tree)).toContain(FIRST_RUN_CONNECTED_HEADING);
    expect(text(tree)).toContain(FIRST_RUN_ADD_ANOTHER_HEADING);
    // The one account is the default by being alone: its row says nothing of it and takes no click.
    const row = accountRow(tree, ANTHROPIC.id);
    expect(row.tag).toBe("div");
    expect(row.attrs.onclick).toBeUndefined();
    expect(text(tree)).not.toContain(FIRST_RUN_DEFAULT_TAG);
    expect(text(tree)).not.toContain(FIRST_RUN_MAKE_DEFAULT_HINT);
    expect(byId(tree, "first-run-next").attrs.disabled).toBe(false);
    // With a provider connected there is nothing to skip.
    expect(nodes(tree).some((node) => node.attrs?.id === "first-run-skip")).toBe(false);
  });

  it("marks the default of two connected accounts, and offers to make the other one the default", () => {
    mocks.accounts = [ANTHROPIC, OPENAI];
    mocks.selectedAccountId = ANTHROPIC.id;
    const tree = render().view();

    const defaultRow = accountRow(tree, ANTHROPIC.id);
    expect(defaultRow.tag).toBe("div");
    expect(defaultRow.attrs.onclick).toBeUndefined();
    expect(text(defaultRow)).toContain(FIRST_RUN_DEFAULT_TAG);
    expect(text(defaultRow)).not.toContain(FIRST_RUN_MAKE_DEFAULT_HINT);
    const otherRow = accountRow(tree, OPENAI.id);
    expect(otherRow.tag).toBe("button");
    expect(text(otherRow)).toContain(FIRST_RUN_MAKE_DEFAULT_HINT);
    expect(text(otherRow)).not.toContain(FIRST_RUN_DEFAULT_TAG);
  });

  it("follows the default to whichever account holds it", () => {
    mocks.accounts = [ANTHROPIC, OPENAI];
    mocks.selectedAccountId = OPENAI.id;
    const tree = render().view();

    expect(text(accountRow(tree, OPENAI.id))).toContain(FIRST_RUN_DEFAULT_TAG);
    expect(accountRow(tree, ANTHROPIC.id).tag).toBe("button");
  });

  it("pins the account whose row is clicked, and draws again once that has landed", async () => {
    const redraw = vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    mocks.accounts = [ANTHROPIC, OPENAI];
    mocks.selectedAccountId = ANTHROPIC.id;

    (accountRow(render().view(), OPENAI.id).attrs.onclick as () => void)();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    expect(mocks.setDefaultAccount.mock.calls).toEqual([[OPENAI.id, true]]);
    expect(mocks.loadAccounts).not.toHaveBeenCalled();
    expect(redraw).toHaveBeenCalled();
  });

  it("reads the accounts again when the pin does not go through, so the tag stays where the default is", async () => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mocks.setDefaultAccount.mockImplementation(() => Promise.reject(new Error("404")));
    mocks.accounts = [ANTHROPIC, OPENAI];
    mocks.selectedAccountId = ANTHROPIC.id;

    (accountRow(render().view(), OPENAI.id).attrs.onclick as () => void)();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    expect(mocks.loadAccounts).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("finishes the step once on Skip and tells the shell to take its cover down", async () => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    const { view, shell } = render();

    (byId(view(), "first-run-skip").attrs.onclick as () => void)();
    (byId(view(), "first-run-skip").attrs.onclick as () => void)();
    await Promise.resolve();
    await Promise.resolve();

    expect(mocks.finish).toHaveBeenCalledTimes(1);
    expect(shell.firstRunProviderStep).toHaveBeenCalledWith(false);
  });

  it("stays, and says so, when leaving does not go through", async () => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    mocks.finish.mockImplementationOnce(() => Promise.reject(new Error("offline")));
    const { view, shell } = render();

    (byId(view(), "first-run-skip").attrs.onclick as () => void)();
    await Promise.resolve();
    await Promise.resolve();

    expect(shell.firstRunProviderStep).not.toHaveBeenCalled();
    expect(text(view())).toContain("That didn't go through. Try again.");
    expect(byId(view(), "first-run-skip").attrs.disabled).toBe(false);
  });
});
