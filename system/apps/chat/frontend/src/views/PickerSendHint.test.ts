// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";

vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

import type { ChatSettings } from "../models/ChatSettings";
import { DEFAULT_CHAT_SETTINGS, resetChatSettingsForTests } from "../models/ChatSettings";
import { PICKER_SEND_HINT_TEXT, showPickerSendHintOnce } from "./PickerSendHint";

/** What the backend holds, which the page's requests read and replace. */
let stored: ChatSettings;
let isReadFailing: boolean;
let writes: ChatSettings[];

async function settle(): Promise<void> {
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
}

function hintText(): string | null {
  return document.querySelector(".coach-mark-text")?.textContent ?? null;
}

function mountSendButton(): HTMLButtonElement {
  const button = document.createElement("button");
  button.className = "message-input-send-button";
  document.body.appendChild(button);
  return button;
}

beforeEach(() => {
  resetChatSettingsForTests();
  stored = { ...DEFAULT_CHAT_SETTINGS };
  isReadFailing = false;
  writes = [];
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  vi.spyOn(m, "request").mockImplementation((async (options: { method: string; body?: ChatSettings }) => {
    if (options.method === "PUT" && options.body !== undefined) {
      stored = options.body;
      writes.push(options.body);
    } else if (isReadFailing) {
      throw new Error("the backend is away");
    }
    return { settings: stored };
  }) as never);
});

afterEach(() => {
  document.querySelector<HTMLButtonElement>(".coach-mark-dismiss")?.click();
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("showPickerSendHintOnce", () => {
  it("points at the send button the first time, and records that the workspace has seen it", async () => {
    mountSendButton();

    showPickerSendHintOnce();
    await settle();

    expect(hintText()).toBe(PICKER_SEND_HINT_TEXT);
    expect(hintText()).toBe("Click this button to send your message");
    expect(writes).toEqual([{ ...DEFAULT_CHAT_SETTINGS, is_picker_send_hint_shown: true }]);
  });

  it("shows nothing in a workspace that has seen it, on this page or any other", async () => {
    stored = { ...DEFAULT_CHAT_SETTINGS, is_picker_send_hint_shown: true };
    mountSendButton();

    showPickerSendHintOnce();
    await settle();
    showPickerSendHintOnce();
    await settle();

    expect(hintText()).toBeNull();
    expect(writes).toEqual([]);
  });

  it("shows it once however many categories are picked", async () => {
    mountSendButton();

    showPickerSendHintOnce();
    showPickerSendHintOnce();
    await settle();
    document.querySelector<HTMLButtonElement>(".coach-mark-dismiss")?.click();
    showPickerSendHintOnce();
    await settle();

    expect(hintText()).toBeNull();
    expect(writes).toHaveLength(1);
  });

  it("goes when the send button is clicked", async () => {
    const sendButton = mountSendButton();
    showPickerSendHintOnce();
    await settle();

    sendButton.click();

    expect(hintText()).toBeNull();
  });

  it("neither shows nor records anything while the settings cannot be read, and shows it once they can", async () => {
    isReadFailing = true;
    mountSendButton();

    showPickerSendHintOnce();
    await settle();
    expect(hintText()).toBeNull();
    expect(writes).toEqual([]);

    isReadFailing = false;
    resetChatSettingsForTests();
    showPickerSendHintOnce();
    await settle();

    expect(hintText()).toBe(PICKER_SEND_HINT_TEXT);
  });

  it("records nothing on a page with no send button to point at", async () => {
    showPickerSendHintOnce();
    await settle();

    expect(hintText()).toBeNull();
    expect(writes).toEqual([]);
  });
});
