// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// vi.mock factories are hoisted above module scope, so what they close over comes from vi.hoisted.
const mocks = vi.hoisted(() => ({
  showsCoachMarks: false,
  openShareSettingsWithCoachMark: vi.fn(),
  showCoachCard: vi.fn(),
  focusComposer: vi.fn(() => true),
}));

vi.mock("../shell", () => ({
  showsCoachMarks: () => mocks.showsCoachMarks,
  openShareSettingsWithCoachMark: mocks.openShareSettingsWithCoachMark,
  showCoachCard: mocks.showCoachCard,
}));
vi.mock("./MessageInput", () => ({ focusComposer: mocks.focusComposer }));

import type { ChatRootEmbedApi } from "../embedApi";
import {
  BUILD_APP_COACH_TEXT,
  CONNECT_DATA_COACH_TEXT,
  canRunSuggestionAction,
  runSuggestionAction,
} from "./suggestion-actions";

/** Frame this page under ``parent`` for the length of a test: a chat root, or the shell, which exposes nothing. */
function framedBy(parent: { chatRootEmbed?: Partial<ChatRootEmbedApi> }): void {
  Object.defineProperty(window, "parent", { value: parent, configurable: true });
}

function composerTextbox(): HTMLTextAreaElement {
  const textbox = document.createElement("textarea");
  textbox.className = "message-input-textbox";
  document.body.appendChild(textbox);
  return textbox;
}

function bubbleText(): string | null {
  return document.querySelector(".coach-mark .coach-mark-text")?.textContent ?? null;
}

beforeEach(() => {
  mocks.showsCoachMarks = false;
  mocks.openShareSettingsWithCoachMark.mockClear();
  mocks.showCoachCard.mockClear();
  mocks.focusComposer.mockClear();
});

afterEach(() => {
  document.querySelector<HTMLElement>(".coach-mark-dismiss")?.click();
  document.body.replaceChildren();
  Object.defineProperty(window, "parent", { value: window, configurable: true });
});

describe("whether this page can do what a suggestion's click asks for", () => {
  it("points at the New chat button only where a chat list that draws the mark frames the page", () => {
    expect(canRunSuggestionAction("new_chat_coach")).toBe(false);

    // The shell frames the page itself: its window exposes no chat root.
    framedBy({});
    expect(canRunSuggestionAction("new_chat_coach")).toBe(false);

    // A chat list loaded before it could draw the mark.
    framedBy({ chatRootEmbed: { chooseProviderForFirstSend: () => undefined } });
    expect(canRunSuggestionAction("new_chat_coach")).toBe(false);

    framedBy({
      chatRootEmbed: { chooseProviderForFirstSend: () => undefined, showNewChatCoachMark: () => undefined },
    });
    expect(canRunSuggestionAction("new_chat_coach")).toBe(true);
  });

  it("reaches the chrome's share panel and permissions button only where a chrome says it draws coach marks", () => {
    expect(canRunSuggestionAction("phone_access")).toBe(false);
    expect(canRunSuggestionAction("connect_data_coach")).toBe(false);

    mocks.showsCoachMarks = true;
    expect(canRunSuggestionAction("phone_access")).toBe(true);
    expect(canRunSuggestionAction("connect_data_coach")).toBe(true);
  });

  it("can always point at its own composer", () => {
    expect(canRunSuggestionAction("build_app_coach")).toBe(true);
  });
});

describe("a click on an action suggestion", () => {
  it("asks the chat list for its coach mark at the New chat button", () => {
    const showNewChatCoachMark = vi.fn();
    framedBy({ chatRootEmbed: { showNewChatCoachMark } });

    runSuggestionAction("new_chat_coach");

    expect(showNewChatCoachMark).toHaveBeenCalledTimes(1);
    // The mark is the chat list's to draw: this page draws none.
    expect(bubbleText()).toBeNull();
  });

  it("asks the chrome for its share panel with a coach mark at its phone-access control", () => {
    runSuggestionAction("phone_access");

    expect(mocks.openShareSettingsWithCoachMark).toHaveBeenCalledTimes(1);
    expect(mocks.openShareSettingsWithCoachMark).toHaveBeenCalledWith("mobile-access");
  });

  it("points a coach mark at the composer's text area, and puts the keyboard in it", () => {
    const textbox = composerTextbox();

    runSuggestionAction("build_app_coach");

    expect(bubbleText()).toBe(BUILD_APP_COACH_TEXT);
    expect(BUILD_APP_COACH_TEXT).toBe(
      "Provide some context on a problem you're facing and tell the agent to build an app to help with it.",
    );
    expect(document.querySelector(".coach-mark-dismiss")?.textContent).toBe("Got it");
    expect(mocks.focusComposer).toHaveBeenCalledTimes(1);

    // It goes when the text area it points at is clicked.
    textbox.click();
    expect(bubbleText()).toBeNull();
  });

  it("draws nothing on a page with no composer", () => {
    runSuggestionAction("build_app_coach");

    expect(bubbleText()).toBeNull();
    expect(mocks.focusComposer).not.toHaveBeenCalled();
  });

  it("asks the shell for a card under the chrome's permissions button, with the words to show", () => {
    runSuggestionAction("connect_data_coach");

    expect(mocks.showCoachCard).toHaveBeenCalledTimes(1);
    expect(mocks.showCoachCard).toHaveBeenCalledWith(CONNECT_DATA_COACH_TEXT, "permissions");
    expect(CONNECT_DATA_COACH_TEXT).toBe(
      "Your connections live here. Ask me to connect any of these services, or any other data you want to pull in.",
    );
  });
});
