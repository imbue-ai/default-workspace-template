import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend's answers without a network call.
const { mockRequest, mockRedraw } = vi.hoisted(() => ({ mockRequest: vi.fn(), mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: mockRedraw } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));
const settingsListeners = vi.hoisted(() => [] as (() => void)[]);
vi.mock("./ChatSettings", () => ({
  addChatSettingsUpdatedListener: (listener: () => void) => settingsListeners.push(listener),
}));

import {
  type NewChatAnswer,
  type NewChatFacts,
  actionSuggestionsOf,
  getNewChatSuggestions,
  reconcileNewChatSuggestions,
  resetNewChatSuggestionsForTests,
  RETRY_DELAY_MS,
} from "./NewChatSuggestions";
import { ALL_FOUR, BUILD_APP, CONNECT_DATA, NEW_CHAT, PHONE } from "./newChatSuggestionFixture";
import type { SuggestionAction } from "./Suggestions";

const CHAT = "agent-1";

/** What the chat app answers, which the page's reads read. */
let offered: NewChatAnswer;
let requests: { method: string; url: string; params?: unknown }[];

/** A chat nobody has spoken in, with new-chat suggestions on and every action within reach. */
function facts(overrides: Partial<NewChatFacts> = {}): NewChatFacts {
  return {
    isRoom: false,
    hasUserMessage: false,
    isEnabled: true,
    canActOn: () => true,
    ...overrides,
  };
}

/** Let the reads the page asked for answer. */
async function settle(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

/** Look at the chat as a draw does, and again once any read it asked for has answered. */
async function look(overrides: Partial<NewChatFacts> = {}): Promise<void> {
  reconcileNewChatSuggestions(CHAT, facts(overrides));
  await settle();
  reconcileNewChatSuggestions(CHAT, facts(overrides));
}

function shownIds(): string[] | null {
  return getNewChatSuggestions(CHAT)?.map((suggestion) => suggestion.suggestion_id) ?? null;
}

beforeEach(() => {
  resetNewChatSuggestionsForTests();
  offered = ALL_FOUR;
  requests = [];
  mockRequest.mockReset();
  mockRequest.mockImplementation(async (options: { method: string; url: string; params?: unknown }) => {
    requests.push(options);
    return offered;
  });
  mockRedraw.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("actionSuggestionsOf", () => {
  it("keeps the suggestions whose action this page knows, in the order answered", () => {
    expect(actionSuggestionsOf(ALL_FOUR)).toEqual([
      { ...NEW_CHAT, is_recommended: false },
      { ...PHONE, is_recommended: false },
      { ...BUILD_APP, is_recommended: false },
      { ...CONNECT_DATA, is_recommended: false },
    ]);
  });

  it("drops a suggestion with an action a newer backend added, with none, or with one that is no name at all", () => {
    const answer: NewChatAnswer = {
      suggestions: [
        { suggestion_id: "watch_video", label: "Watch the video", action: "open_video" },
        BUILD_APP,
        { suggestion_id: "reply", label: "Tell me more", action: null },
        { suggestion_id: "bare", label: "Bare" },
        { suggestion_id: "proto", label: "Proto", action: "toString" },
      ],
    };
    expect(actionSuggestionsOf(answer).map((suggestion) => suggestion.suggestion_id)).toEqual(["build_app"]);
  });
});

describe("the new-chat strip", () => {
  it("is there on a chat nobody has spoken in, with what the chat app offers that chat", async () => {
    await look();

    expect(requests).toEqual([{ method: "GET", url: "/api/suggestions/new-chat", params: { chat_id: CHAT } }]);
    expect(getNewChatSuggestions(CHAT)?.map((suggestion) => suggestion.action)).toEqual<SuggestionAction[]>([
      "new_chat_coach",
      "phone_access",
      "build_app_coach",
    ]);
    // Later looks read nothing more.
    await look();
    expect(requests).toHaveLength(1);
  });

  it("is absent on a chat that had a message when the page first looked, and waits while the page cannot tell", async () => {
    await look({ hasUserMessage: null });
    expect(requests).toEqual([]);

    await look({ hasUserMessage: true });
    expect(getNewChatSuggestions(CHAT)).toBeNull();
    expect(requests).toEqual([]);

    // A chat that turns out empty later (its transcript was evicted, say) was not new when the page met it.
    await look({ hasUserMessage: false });
    expect(getNewChatSuggestions(CHAT)).toBeNull();
    expect(requests).toEqual([]);
  });

  it("shows the first three suggestions the page can act on, skipping the others before it counts", async () => {
    await look({ canActOn: (suggestion) => suggestion.action !== "phone_access" });

    expect(shownIds()).toEqual(["new_chat", "build_app", "connect_data"]);

    await look({ canActOn: (suggestion) => suggestion.action === "build_app_coach" });
    expect(shownIds()).toEqual(["build_app"]);
  });

  it("is absent when the chat app offers nothing, as in the first-run chat, or nothing the page can act on", async () => {
    offered = { suggestions: [] };
    await look();
    expect(getNewChatSuggestions(CHAT)).toBeNull();

    resetNewChatSuggestionsForTests();
    offered = ALL_FOUR;
    await look({ canActOn: () => false });
    expect(getNewChatSuggestions(CHAT)).toBeNull();
  });

  it("goes for good on the chat's first message", async () => {
    await look();
    expect(shownIds()).toEqual(["new_chat", "phone_access", "build_app"]);

    reconcileNewChatSuggestions(CHAT, facts({ hasUserMessage: true }));
    expect(getNewChatSuggestions(CHAT)).toBeNull();

    // Nothing brings it back: not a later look that finds no message, nor a switch changed somewhere.
    await look({ hasUserMessage: false });
    for (const listener of settingsListeners) listener();
    await look({ hasUserMessage: false });
    expect(getNewChatSuggestions(CHAT)).toBeNull();
    expect(requests).toHaveLength(1);
  });

  it("goes at once when new chat suggestions are switched off, and reads nothing while they are", async () => {
    await look();

    reconcileNewChatSuggestions(CHAT, facts({ isEnabled: false }));
    expect(getNewChatSuggestions(CHAT)).toBeNull();

    resetNewChatSuggestionsForTests();
    requests = [];
    await look({ isEnabled: false });
    expect(requests).toEqual([]);

    // Switched on: the read the first look asked for goes out now.
    await look();
    expect(requests).toHaveLength(1);
    expect(shownIds()).toEqual(["new_chat", "phone_access", "build_app"]);
  });

  it("reads again what it shows when a switch changes somewhere, keeping the strip up meanwhile", async () => {
    await look();
    offered = { suggestions: [BUILD_APP] };

    for (const listener of settingsListeners) listener();
    reconcileNewChatSuggestions(CHAT, facts());

    expect(shownIds()).toEqual(["new_chat", "phone_access", "build_app"]);
    await settle();
    reconcileNewChatSuggestions(CHAT, facts());
    expect(requests).toHaveLength(2);
    expect(shownIds()).toEqual(["build_app"]);
  });

  it("asks again after a read that failed, once the retry delay has passed", async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mockRequest.mockRejectedValueOnce(new Error("503"));

    await look();
    expect(getNewChatSuggestions(CHAT)).toBeNull();
    expect(warn).toHaveBeenCalledTimes(1);
    await look();
    expect(requests).toHaveLength(0);

    vi.advanceTimersByTime(RETRY_DELAY_MS);
    await look();
    expect(requests).toHaveLength(1);
    expect(shownIds()).toEqual(["new_chat", "phone_access", "build_app"]);
  });

  it("is never drawn in a room", async () => {
    await look({ isRoom: true });

    expect(getNewChatSuggestions(CHAT)).toBeNull();
    expect(requests).toEqual([]);
  });
});
