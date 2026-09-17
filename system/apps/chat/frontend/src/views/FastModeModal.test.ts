// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";

const { settingsState, settingsWrites, fastModeState, choices } = vi.hoisted(() => ({
  settingsState: {
    settings: null as {
      fast_mode_default: string;
      fast_mode_turn_limit: number;
      is_fast_mode_notice_shown: boolean;
    } | null,
  },
  settingsWrites: [] as unknown[],
  fastModeState: { state: null as { mode: string; is_switched: boolean } | null },
  choices: [] as [string, string][],
}));
vi.mock("../models/ChatSettings", () => ({
  DEFAULT_CHAT_SETTINGS: { fast_mode_default: "auto", fast_mode_turn_limit: 5, is_fast_mode_notice_shown: false },
  getChatSettings: () => settingsState.settings,
  ensureChatSettings: () => Promise.resolve(settingsState.settings),
  updateChatSettings: (next: unknown) => {
    settingsWrites.push(next);
    return Promise.resolve(next);
  },
}));
vi.mock("../models/FastMode", () => ({
  getFastModeState: () => fastModeState.state,
  ensureFastModeState: () => Promise.resolve(fastModeState.state),
}));
vi.mock("../models/Response", () => ({ getEventsForChat: () => [] }));
vi.mock("./fast-mode-limit", () => ({
  chooseFastMode: (chatId: string, mode: string) => {
    choices.push([chatId, mode]);
  },
}));

import { FastModeModal, fastModeDetail } from "./FastModeModal";

const ROOT = () => document.getElementById("root") as HTMLElement;
const closes: number[] = [];

function render(): void {
  m.render(ROOT(), m(FastModeModal, { chatId: "a1", onClose: () => closes.push(1) }));
}

function click(selector: string): void {
  const node = document.querySelector<HTMLElement>(selector);
  if (node === null) throw new Error(`no ${selector} on screen`);
  node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  render();
}

beforeEach(() => {
  document.body.innerHTML = '<div id="root"></div>';
  settingsState.settings = { fast_mode_default: "auto", fast_mode_turn_limit: 5, is_fast_mode_notice_shown: false };
  fastModeState.state = { mode: "auto", is_switched: false };
  settingsWrites.length = 0;
  choices.length = 0;
  closes.length = 0;
});

describe("fastModeDetail", () => {
  it("explains each mode, auto with the limit it runs to", () => {
    expect(fastModeDetail("off", 5)).toBe("Standard speed for the whole chat.");
    expect(fastModeDetail("on", 5)).toBe("Fast for the whole chat.");
    expect(fastModeDetail("auto", 1)).toBe("Fast for the first 1 turn, then standard speed.");
    expect(fastModeDetail("auto", 4)).toBe("Fast for the first 4 turns, then standard speed.");
  });
});

describe("FastModeModal", () => {
  it("marks the chat's mode, picks another on a press, and closes on Done", () => {
    render();
    expect(document.querySelector('[data-fast-mode="auto"]')?.getAttribute("aria-checked")).toBe("true");
    expect(document.querySelector('[data-fast-mode="on"]')?.getAttribute("aria-checked")).toBe("false");
    click('[data-fast-mode="off"]');
    expect(choices).toEqual([["a1", "off"]]);
    // Pressing the mode the chat is already in chooses nothing again.
    click('[data-fast-mode="auto"]');
    expect(choices).toEqual([["a1", "off"]]);
    click(".fast-mode-done");
    expect(closes).toEqual([1]);
  });

  it("offers the turn limit only under auto, and writes a changed one to the settings", () => {
    render();
    const limit = document.querySelector<HTMLInputElement>(".fast-limit-input");
    if (limit === null) throw new Error("no turn-limit field");
    expect(limit.value).toBe("5");
    limit.value = "2";
    limit.dispatchEvent(new Event("input", { bubbles: true }));
    render();
    expect(limit.value).toBe("2");
    limit.dispatchEvent(new Event("change", { bubbles: true }));
    expect(settingsWrites).toEqual([
      { fast_mode_default: "auto", fast_mode_turn_limit: 2, is_fast_mode_notice_shown: false },
    ]);
    // An emptied field or zero is not a limit.
    limit.value = "";
    limit.dispatchEvent(new Event("change", { bubbles: true }));
    limit.value = "0";
    limit.dispatchEvent(new Event("change", { bubbles: true }));
    expect(settingsWrites).toHaveLength(1);

    fastModeState.state = { mode: "on", is_switched: false };
    render();
    expect(document.querySelector(".fast-limit-input")).toBeNull();
  });

  it("says when auto has switched the chat and makes the chat's mode the default for new chats on request", () => {
    fastModeState.state = { mode: "auto", is_switched: true };
    render();
    expect(document.querySelector('[data-fast-mode="auto"]')?.textContent).toContain("(off now)");
    // Auto is already the default, so the box is checked and inert.
    const defaultBox = document.querySelector<HTMLInputElement>(".fast-mode-default input");
    if (defaultBox === null) throw new Error("no default checkbox");
    expect(defaultBox.checked).toBe(true);
    expect(defaultBox.disabled).toBe(true);

    fastModeState.state = { mode: "on", is_switched: false };
    render();
    const box = document.querySelector<HTMLInputElement>(".fast-mode-default input");
    if (box === null) throw new Error("no default checkbox");
    expect(box.checked).toBe(false);
    expect(document.querySelector(".fast-mode-default")?.textContent).toContain("Use On for new chats");
    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    expect(settingsWrites).toEqual([
      { fast_mode_default: "on", fast_mode_turn_limit: 5, is_fast_mode_notice_shown: false },
    ]);
  });
});
