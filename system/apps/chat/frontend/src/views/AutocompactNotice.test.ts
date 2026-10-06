import { beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import type { ChatSettings } from "../models/ChatSettings";

// The notice reads and writes the workspace settings; the mock keeps them in a holder whose
// writes land at once, as the backend's answer would.
const settingsState = vi.hoisted(() => ({
  settings: null as ChatSettings | null,
  ensureChatSettings: vi.fn(),
  updateChatSettings: vi.fn(),
}));
vi.mock("../models/ChatSettings", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/ChatSettings")>()),
  getChatSettings: () => settingsState.settings,
  ensureChatSettings: settingsState.ensureChatSettings,
  updateChatSettings: settingsState.updateChatSettings,
}));

import { DEFAULT_CHAT_SETTINGS } from "../models/ChatSettings";
import { AUTOCOMPACT_NOTICE_TEXT, AutocompactNotice } from "./AutocompactNotice";

type AnyVnode = { tag?: unknown; attrs?: Record<string, unknown>; children?: unknown; text?: unknown };

function render(): AnyVnode | null {
  const component = AutocompactNotice();
  return component.view(m(AutocompactNotice) as never) as AnyVnode | null;
}

function allText(node: unknown): string {
  if (node == null) return "";
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(allText).join("");
  if (typeof node === "object") {
    const v = node as AnyVnode;
    return (typeof v.text === "string" ? v.text : "") + allText(v.children);
  }
  return "";
}

/** The dismiss button's vnode (a Button component, so found by the marker in its `extra`). */
function findDismiss(node: unknown): AnyVnode | null {
  if (node == null || typeof node !== "object") return null;
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findDismiss(child);
      if (found !== null) return found;
    }
    return null;
  }
  const v = node as AnyVnode;
  if (typeof v.attrs?.extra === "string" && v.attrs.extra.includes("autocompact-notice-dismiss")) return v;
  return findDismiss(v.children);
}

describe("AutocompactNotice", () => {
  beforeEach(() => {
    settingsState.settings = null;
    settingsState.ensureChatSettings.mockClear();
    settingsState.updateChatSettings.mockReset();
    settingsState.updateChatSettings.mockImplementation(async (next: ChatSettings) => {
      settingsState.settings = next;
      return next;
    });
  });

  it("waits for the settings rather than showing for the defaults, and asks for them", () => {
    expect(render()).toBeNull();
    expect(settingsState.ensureChatSettings).toHaveBeenCalled();
  });

  it("shows until dismissed, records the dismissal on the settings, and stays gone", () => {
    settingsState.settings = { ...DEFAULT_CHAT_SETTINGS, fast_mode_turn_limit: 7 };
    const shown = render();
    expect(allText(shown)).toContain(AUTOCOMPACT_NOTICE_TEXT);
    expect(AUTOCOMPACT_NOTICE_TEXT).toBe(
      "Idle chats now compact automatically to keep replies fast and cheap. Turn this off per chat, or for new " +
        "chats, under Auto-compact in the model menu.",
    );
    // Showing it is not seeing it: nothing is written until the user dismisses it.
    expect(render()).not.toBeNull();
    expect(settingsState.updateChatSettings).not.toHaveBeenCalled();

    (findDismiss(shown)?.attrs?.onclick as () => void)();

    expect(settingsState.updateChatSettings).toHaveBeenCalledWith({
      ...DEFAULT_CHAT_SETTINGS,
      fast_mode_turn_limit: 7,
      is_autocompact_notice_shown: true,
    });
    expect(render()).toBeNull();
  });

  it("stays hidden in a workspace that already dismissed it", () => {
    settingsState.settings = { ...DEFAULT_CHAT_SETTINGS, is_autocompact_notice_shown: true };
    expect(render()).toBeNull();
  });
});
