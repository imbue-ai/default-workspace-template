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
  type ActionPlacement,
  type ActivityAnswer,
  type ActivityFacts,
  actionSuggestionsOf,
  collapseActionStrip,
  getActionStrip,
  reconcileActivitySuggestions,
  recordActionSuggestionClicked,
  resetActivitySuggestionsForTests,
  RETRY_DELAY_MS,
  toggleActionStrip,
} from "./ActivitySuggestions";
import type { SuggestionAction } from "./Suggestions";

const CHAT = "agent-1";

const NEW_CHAT = {
  suggestion_id: "new_chat",
  label: "Work in parallel by starting a new chat",
  action: "new_chat_coach",
};
const PHONE = { suggestion_id: "phone_access", label: "Access Studio from your phone", action: "phone_access" };
const BUILD_APP = { suggestion_id: "build_app", label: "Build an app", action: "build_app_coach" };
const CONNECT_DATA = { suggestion_id: "connect_data", label: "Connect some data", action: "connect_data_coach" };
const ALL_FOUR: ActivityAnswer = { suggestions: [NEW_CHAT, PHONE, BUILD_APP, CONNECT_DATA] };

/** What the chat app answers each placement, which the page's reads read. */
let offered: Record<ActionPlacement, ActivityAnswer>;
let requests: { method: string; url: string; params?: unknown; body?: unknown }[];

/** A chat somebody has spoken in, whose turn is done, with both placements on and every action within reach. */
function facts(overrides: Partial<ActivityFacts> = {}): ActivityFacts {
  return {
    isRoom: false,
    isTurnDone: true,
    hasUserMessage: true,
    isEnabled: { activity: true, new_chat: true },
    canActOn: () => true,
    ...overrides,
  };
}

/** Let the reads the page asked for answer. */
async function settle(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

/** Look at the chat as a draw does, and again once any read it asked for has answered. */
async function look(overrides: Partial<ActivityFacts> = {}): Promise<void> {
  reconcileActivitySuggestions(CHAT, facts(overrides));
  await settle();
  reconcileActivitySuggestions(CHAT, facts(overrides));
}

function shownIds(placement: ActionPlacement): string[] | null {
  return getActionStrip(CHAT, placement)?.suggestions.map((suggestion) => suggestion.suggestion_id) ?? null;
}

function isOpen(placement: ActionPlacement): boolean | null {
  return getActionStrip(CHAT, placement)?.isOpen ?? null;
}

function readsOf(placement: ActionPlacement): number {
  return requests.filter(
    (request) => request.method === "GET" && (request.params as { placement: string }).placement === placement,
  ).length;
}

beforeEach(() => {
  resetActivitySuggestionsForTests();
  offered = { activity: ALL_FOUR, new_chat: ALL_FOUR };
  requests = [];
  mockRequest.mockReset();
  mockRequest.mockImplementation(
    async (options: { method: string; url: string; params?: unknown; body?: unknown }) => {
      requests.push(options);
      if (options.method === "POST") return {};
      return offered[(options.params as { placement: ActionPlacement }).placement];
    },
  );
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
    const answer: ActivityAnswer = {
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

describe("the activity strip", () => {
  it("is absent on a chat no turn has been seen starting in, and asks the chat app nothing", async () => {
    await look();

    expect(getActionStrip(CHAT, "activity")).toBeNull();
    expect(readsOf("activity")).toBe(0);
  });

  it("opens when a turn starts, with what the chat app offers this chat, read then", async () => {
    await look();
    await look({ isTurnDone: false });

    expect(requests).toEqual([
      { method: "GET", url: "/api/suggestions/activity", params: { placement: "activity", chat_id: CHAT } },
    ]);
    expect(isOpen("activity")).toBe(true);
    expect(getActionStrip(CHAT, "activity")?.suggestions.map((suggestion) => suggestion.action)).toEqual<
      SuggestionAction[]
    >(["new_chat_coach", "phone_access", "build_app_coach"]);
  });

  it("opens on a page that first looks while a reply is being worked on", async () => {
    await look({ isTurnDone: false });

    expect(isOpen("activity")).toBe(true);
  });

  it("waits for a page that cannot tell yet whether a reply is being worked on", async () => {
    await look({ isTurnDone: null });
    expect(readsOf("activity")).toBe(0);

    await look({ isTurnDone: true });
    expect(getActionStrip(CHAT, "activity")).toBeNull();
    expect(readsOf("activity")).toBe(0);
  });

  it("shows the first three suggestions the page can act on, skipping the others before it counts", async () => {
    await look({ isTurnDone: false, canActOn: (suggestion) => suggestion.action !== "phone_access" });

    expect(shownIds("activity")).toEqual(["new_chat", "build_app", "connect_data"]);

    await look({ isTurnDone: false, canActOn: (suggestion) => suggestion.action === "build_app_coach" });
    expect(shownIds("activity")).toEqual(["build_app"]);
  });

  it("is absent when the chat app offers nothing, or nothing the page can act on", async () => {
    offered.activity = { suggestions: [] };
    await look({ isTurnDone: false });
    expect(getActionStrip(CHAT, "activity")).toBeNull();

    resetActivitySuggestionsForTests();
    offered.activity = ALL_FOUR;
    await look({ isTurnDone: false, canActOn: () => false });
    expect(getActionStrip(CHAT, "activity")).toBeNull();
  });

  it("closes to its toggle when the turn ends, and stays there until the next turn opens it with a fresh read", async () => {
    await look({ isTurnDone: false });
    await look({ isTurnDone: true });

    expect(isOpen("activity")).toBe(false);
    expect(shownIds("activity")).toEqual(["new_chat", "phone_access", "build_app"]);
    expect(readsOf("activity")).toBe(1);

    // A second chat was started meanwhile, so the chat app no longer offers the first suggestion.
    offered.activity = { suggestions: [PHONE, BUILD_APP, CONNECT_DATA] };
    await look({ isTurnDone: false });

    expect(readsOf("activity")).toBe(2);
    expect(isOpen("activity")).toBe(true);
    expect(shownIds("activity")).toEqual(["phone_access", "build_app", "connect_data"]);
  });

  it("shows nothing of the turn before while the read of the turn that just started is out", async () => {
    await look({ isTurnDone: false });
    await look({ isTurnDone: true });

    reconcileActivitySuggestions(CHAT, facts({ isTurnDone: false }));

    expect(getActionStrip(CHAT, "activity")).toBeNull();
    await settle();
    reconcileActivitySuggestions(CHAT, facts({ isTurnDone: false }));
    expect(isOpen("activity")).toBe(true);
  });

  it("opens and collapses from its toggle, and collapses when another strip takes its place", async () => {
    await look({ isTurnDone: false });

    toggleActionStrip(CHAT, "activity");
    reconcileActivitySuggestions(CHAT, facts({ isTurnDone: false }));
    expect(isOpen("activity")).toBe(false);

    toggleActionStrip(CHAT, "activity");
    reconcileActivitySuggestions(CHAT, facts({ isTurnDone: false }));
    expect(isOpen("activity")).toBe(true);

    collapseActionStrip(CHAT, "activity");
    collapseActionStrip(CHAT, "activity");
    reconcileActivitySuggestions(CHAT, facts({ isTurnDone: false }));
    expect(isOpen("activity")).toBe(false);
  });

  it("goes at once when activity suggestions are switched off, and reads nothing while they are", async () => {
    await look({ isTurnDone: false });

    reconcileActivitySuggestions(CHAT, facts({ isTurnDone: false, isEnabled: { activity: false, new_chat: true } }));
    expect(getActionStrip(CHAT, "activity")).toBeNull();

    await look({ isTurnDone: true, isEnabled: { activity: false, new_chat: true } });
    await look({ isTurnDone: false, isEnabled: { activity: false, new_chat: true } });
    expect(readsOf("activity")).toBe(1);

    // Switched back on: the read the turn's start asked for goes out now.
    await look({ isTurnDone: false });
    expect(readsOf("activity")).toBe(2);
    expect(isOpen("activity")).toBe(true);
  });

  it("reads again what a placement shows when a switch changes somewhere, keeping the strip up meanwhile", async () => {
    await look({ isTurnDone: false });
    offered.activity = { suggestions: [BUILD_APP] };

    for (const listener of settingsListeners) listener();
    reconcileActivitySuggestions(CHAT, facts({ isTurnDone: false }));

    expect(shownIds("activity")).toEqual(["new_chat", "phone_access", "build_app"]);
    await settle();
    reconcileActivitySuggestions(CHAT, facts({ isTurnDone: false }));
    expect(readsOf("activity")).toBe(2);
    expect(shownIds("activity")).toEqual(["build_app"]);
  });

  it("asks again after a read that failed, once the retry delay has passed", async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mockRequest.mockRejectedValueOnce(new Error("503"));

    await look({ isTurnDone: false });
    expect(getActionStrip(CHAT, "activity")).toBeNull();
    expect(warn).toHaveBeenCalledTimes(1);
    await look({ isTurnDone: false });
    expect(requests).toHaveLength(0);

    vi.advanceTimersByTime(RETRY_DELAY_MS);
    await look({ isTurnDone: false });
    expect(readsOf("activity")).toBe(1);
    expect(isOpen("activity")).toBe(true);
  });

  it("is never drawn in a room", async () => {
    await look({ isRoom: true, isTurnDone: false, hasUserMessage: false });

    expect(getActionStrip(CHAT, "activity")).toBeNull();
    expect(getActionStrip(CHAT, "new_chat")).toBeNull();
    expect(requests).toEqual([]);
  });
});

describe("the new-chat strip", () => {
  it("is open on a chat nobody has spoken in, with what the chat app offers a new chat", async () => {
    await look({ hasUserMessage: false });

    expect(requests).toEqual([
      { method: "GET", url: "/api/suggestions/activity", params: { placement: "new_chat", chat_id: CHAT } },
    ]);
    expect(isOpen("new_chat")).toBe(true);
    expect(shownIds("new_chat")).toEqual(["new_chat", "phone_access", "build_app"]);
    expect(getActionStrip(CHAT, "activity")).toBeNull();
  });

  it("is absent on a chat that had a message when the page first looked, and waits while the page cannot tell", async () => {
    await look({ hasUserMessage: null });
    expect(readsOf("new_chat")).toBe(0);

    await look({ hasUserMessage: true });
    expect(getActionStrip(CHAT, "new_chat")).toBeNull();
    expect(readsOf("new_chat")).toBe(0);

    // A chat that turns out empty later (its transcript was evicted, say) was not new when the page met it.
    await look({ hasUserMessage: false });
    expect(getActionStrip(CHAT, "new_chat")).toBeNull();
  });

  it("is absent when the chat app offers a new chat nothing, as it does in the first-run chat", async () => {
    offered.new_chat = { suggestions: [] };
    await look({ hasUserMessage: false });

    expect(getActionStrip(CHAT, "new_chat")).toBeNull();
  });

  it("is dropped on the chat's first message when the activity strip opens with the same suggestions", async () => {
    await look({ hasUserMessage: false });

    // The first draw after the send: the activity strip's read is out, and neither strip is drawn.
    reconcileActivitySuggestions(CHAT, facts({ hasUserMessage: true, isTurnDone: false }));
    expect(getActionStrip(CHAT, "new_chat")).toBeNull();
    expect(getActionStrip(CHAT, "activity")).toBeNull();

    await settle();
    reconcileActivitySuggestions(CHAT, facts({ hasUserMessage: true, isTurnDone: false }));
    expect(isOpen("activity")).toBe(true);
    expect(getActionStrip(CHAT, "new_chat")).toBeNull();

    // Dropped for good: the turn's end, and a later turn with other suggestions, do not bring it back.
    await look({ isTurnDone: true });
    offered.activity = { suggestions: [BUILD_APP] };
    await look({ isTurnDone: false });
    expect(getActionStrip(CHAT, "new_chat")).toBeNull();
  });

  it("collapses to its toggle on the first message when the activity strip opens with other suggestions", async () => {
    await look({ hasUserMessage: false });
    offered.activity = { suggestions: [PHONE, BUILD_APP, CONNECT_DATA] };

    await look({ hasUserMessage: true, isTurnDone: false });

    expect(isOpen("new_chat")).toBe(false);
    expect(shownIds("new_chat")).toEqual(["new_chat", "phone_access", "build_app"]);
    expect(isOpen("activity")).toBe(true);

    toggleActionStrip(CHAT, "new_chat");
    reconcileActivitySuggestions(CHAT, facts({ isTurnDone: false }));
    expect(isOpen("new_chat")).toBe(true);
  });

  it("collapses on the first message when activity suggestions are off or offer nothing, with no wait", async () => {
    await look({ hasUserMessage: false, isEnabled: { activity: false, new_chat: true } });

    reconcileActivitySuggestions(
      CHAT,
      facts({ hasUserMessage: true, isTurnDone: false, isEnabled: { activity: false, new_chat: true } }),
    );
    expect(isOpen("new_chat")).toBe(false);

    resetActivitySuggestionsForTests();
    offered.activity = { suggestions: [] };
    await look({ hasUserMessage: false });
    await look({ hasUserMessage: true, isTurnDone: false });
    expect(isOpen("new_chat")).toBe(false);
    expect(getActionStrip(CHAT, "activity")).toBeNull();
  });

  it("is dropped later too, the first time the activity strip shows the same suggestions", async () => {
    await look({ hasUserMessage: false, isEnabled: { activity: false, new_chat: true } });
    await look({ hasUserMessage: true, isTurnDone: false, isEnabled: { activity: false, new_chat: true } });
    expect(isOpen("new_chat")).toBe(false);

    // Activity suggestions are switched on while the turn runs.
    await look({ isTurnDone: false });

    expect(isOpen("activity")).toBe(true);
    expect(getActionStrip(CHAT, "new_chat")).toBeNull();
  });

  it("goes at once when new chat suggestions are switched off", async () => {
    await look({ hasUserMessage: false });

    reconcileActivitySuggestions(
      CHAT,
      facts({ hasUserMessage: false, isEnabled: { activity: true, new_chat: false } }),
    );

    expect(getActionStrip(CHAT, "new_chat")).toBeNull();
  });
});

describe("a click on an action suggestion", () => {
  it("is recorded with the chat app, and what it offers is read again for every strip the chat has", async () => {
    await look({ hasUserMessage: false });
    offered.activity = { suggestions: [PHONE, BUILD_APP, CONNECT_DATA] };
    await look({ hasUserMessage: true, isTurnDone: false });
    requests = [];
    // The click retired the suggestion.
    offered = {
      activity: { suggestions: [PHONE, BUILD_APP] },
      new_chat: { suggestions: [NEW_CHAT, PHONE, BUILD_APP] },
    };

    await recordActionSuggestionClicked(CHAT, "connect_data");
    reconcileActivitySuggestions(CHAT, facts({ isTurnDone: false }));

    expect(requests).toEqual([
      { method: "POST", url: "/api/suggestions/activity/clicked", body: { suggestion_id: "connect_data" } },
      { method: "GET", url: "/api/suggestions/activity", params: { placement: "activity", chat_id: CHAT } },
      { method: "GET", url: "/api/suggestions/activity", params: { placement: "new_chat", chat_id: CHAT } },
    ]);
    expect(shownIds("activity")).toEqual(["phone_access", "build_app"]);
  });

  it("is followed by the read even when the chat app could not be told of it", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    await look({ isTurnDone: false });
    requests = [];
    mockRequest.mockRejectedValueOnce(new Error("400"));

    await recordActionSuggestionClicked(CHAT, "build_app");

    expect(warn).toHaveBeenCalledTimes(1);
    expect(requests).toEqual([
      { method: "GET", url: "/api/suggestions/activity", params: { placement: "activity", chat_id: CHAT } },
    ]);
  });
});
