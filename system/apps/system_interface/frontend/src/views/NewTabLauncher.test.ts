// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The chat tile's provider picker reads the account list; the launcher itself never needs one.
vi.mock("../models/Providers", () => ({
  getAccounts: vi.fn(() => []),
  getSelectedAccount: vi.fn(() => ({ id: "acct-1", label: "Anthropic" })),
  openProviderChooser: vi.fn(),
  selectAccount: vi.fn(),
}));

import m from "mithril";

import { appRecord } from "../testing/records";
import {
  NewTabLauncher,
  appsInRows,
  buildLauncherSections,
  filterRowsByApp,
  formatRecency,
  sortRowsByRecency,
} from "./NewTabLauncher";
import type { LauncherRow, NewTabLauncherAttrs } from "./NewTabLauncher";

function row(
  address: string,
  appName: string,
  lastActiveMs: number | null,
  overrides: Partial<LauncherRow> = {},
): LauncherRow {
  return {
    address,
    appName,
    appDisplayName: appName[0].toUpperCase() + appName.slice(1),
    label: address,
    status: "idle",
    lastActiveMs,
    ...overrides,
  };
}

const MACHINE = [
  row("app:terminal?instance=t1", "terminal", 1_000),
  row("app:chat?instance=c1", "chat", 5_000),
  row("app:files", "files", null),
];

describe("buildLauncherSections", () => {
  it("splits a project's view into its tab set and the rest of the machine", () => {
    const sections = buildLauncherSections(MACHINE, [MACHINE[1]], false);
    expect(sections.map((section) => section.key)).toEqual(["in-project", "on-machine"]);
    expect(sections[0].rows.map((r) => r.address)).toEqual(["app:chat?instance=c1"]);
    expect(sections[1].rows.map((r) => r.address)).toEqual(["app:terminal?instance=t1", "app:files"]);
  });

  it("gives Everything the single machine-wide table", () => {
    const sections = buildLauncherSections(MACHINE, [MACHINE[1]], true);
    expect(sections.map((section) => section.key)).toEqual(["on-machine"]);
    expect(sections[0].rows.length).toBe(3);
  });
});

describe("row helpers", () => {
  it("hides the apps a filter unchecked", () => {
    expect(filterRowsByApp(MACHINE, new Set(["chat"])).map((r) => r.appName)).toEqual(["terminal", "files"]);
  });

  it("orders most recent first with unknown recency last", () => {
    expect(sortRowsByRecency(MACHINE).map((r) => r.appName)).toEqual(["chat", "terminal", "files"]);
  });

  it("lists the apps present once each, in first-seen order", () => {
    expect(appsInRows([...MACHINE, MACHINE[0]]).map((entry) => entry.name)).toEqual(["terminal", "chat", "files"]);
  });

  it("formats recency coarsely", () => {
    const now = 10 * 24 * 60 * 60 * 1000;
    expect(formatRecency(null, now)).toBe("—");
    expect(formatRecency(now + 5, now)).toBe("just now");
    expect(formatRecency(now - 90_000, now)).toBe("1m ago");
    expect(formatRecency(now - 3 * 60 * 60 * 1000, now)).toBe("3h ago");
    expect(formatRecency(now - 2 * 24 * 60 * 60 * 1000, now)).toBe("2d ago");
    expect(formatRecency(now - 8 * 24 * 60 * 60 * 1000, now)).toBe("last week");
    expect(formatRecency(0, 30 * 24 * 60 * 60 * 1000)).toBe("4w ago");
  });
});

describe("NewTabLauncher", () => {
  let root: HTMLElement;

  beforeEach(() => {
    root = document.createElement("div");
    document.body.appendChild(root);
  });

  afterEach(() => {
    m.mount(root, null);
    root.remove();
  });

  function mount(overrides: Partial<NewTabLauncherAttrs>): NewTabLauncherAttrs {
    const attrs: NewTabLauncherAttrs = {
      tiles: [
        { app: appRecord("chat", { critical: true }), action: { id: "new", label: "New Chat" } },
        { app: appRecord("terminal"), action: { id: "new", label: "New terminal" } },
      ],
      rows: MACHINE,
      memberRows: [MACHINE[1]],
      isEverything: false,
      nowMs: 10_000,
      onRunAction: vi.fn(),
      onOpenRow: vi.fn(),
      ...overrides,
    };
    m.mount(root, { view: () => m(NewTabLauncher, attrs) });
    return attrs;
  }

  it("runs a tile's action, passing the chat tile the picked provider account", () => {
    const attrs = mount({});
    root.querySelector<HTMLElement>('[data-launch="terminal:new"]')!.click();
    expect(attrs.onRunAction).toHaveBeenCalledWith(expect.objectContaining({ name: "terminal" }), "new", {});
    root.querySelector<HTMLElement>('[data-launch="chat:new"]')!.click();
    expect(attrs.onRunAction).toHaveBeenCalledWith(expect.objectContaining({ name: "chat" }), "new", {
      account_id: "acct-1",
    });
  });

  it("opens a row from either table through the same callback", () => {
    const attrs = mount({});
    const sections = Array.from(root.querySelectorAll<HTMLElement>("[data-section]"));
    expect(sections.map((section) => section.dataset.section)).toEqual(["in-project", "on-machine"]);
    root.querySelector<HTMLElement>('[data-address="app:files"]')!.click();
    expect(attrs.onOpenRow).toHaveBeenCalledWith(expect.objectContaining({ address: "app:files" }));
  });

  it("stands the tiles down while a create is in flight", () => {
    const attrs = mount({ isAwaitingCreate: true });
    expect(root.textContent).toContain("Starting…");
    root.querySelector<HTMLElement>('[data-launch="terminal:new"]')!.click();
    expect(attrs.onRunAction).not.toHaveBeenCalled();
  });

  it("filters one table by app without touching the other", () => {
    mount({ isEverything: true });
    root.querySelector<HTMLElement>('[data-section="on-machine"] button[aria-expanded]')!.click();
    m.redraw.sync();
    const checkbox = Array.from(root.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))[0];
    checkbox.dispatchEvent(new Event("change"));
    m.redraw.sync();
    expect(root.querySelectorAll(".new-tab-launcher-row").length).toBe(2);
  });
});
