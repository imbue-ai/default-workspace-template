// @vitest-environment jsdom
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// vi.mock factories are hoisted above module scope, so what they close over comes from vi.hoisted.
const mocks = vi.hoisted(() => ({
  chat: undefined as unknown,
  provisional: undefined as unknown,
  isLaunchInFlight: false,
  isRoom: false,
  events: [] as unknown[],
  hasMoreAfter: false,
  isTranscriptLoaded: true,
  submitSuggestion: vi.fn(),
  opensWorkspaceSettings: false,
  openWorkspaceSettings: vi.fn(),
  suggestionsUpdatedListeners: [] as ((chatId: string) => void)[],
}));

vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));
vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: () => mocks.chat,
  getProvisionalChat: () => mocks.provisional,
  isLaunchInFlight: () => mocks.isLaunchInFlight,
  isRoomChat: () => mocks.isRoom,
  addSuggestionsUpdatedListener: (listener: (chatId: string) => void) => {
    mocks.suggestionsUpdatedListeners.push(listener);
  },
  removeSuggestionsUpdatedListener: (listener: (chatId: string) => void) => {
    mocks.suggestionsUpdatedListeners = mocks.suggestionsUpdatedListeners.filter((entry) => entry !== listener);
  },
}));
vi.mock("../models/Response", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Response")>()),
  getEventsForChat: () => mocks.events,
  hasMoreAfter: () => mocks.hasMoreAfter,
  isTranscriptLoaded: () => mocks.isTranscriptLoaded,
}));
vi.mock("../shell", () => ({
  opensWorkspaceSettings: () => mocks.opensWorkspaceSettings,
  openWorkspaceSettings: mocks.openWorkspaceSettings,
  showsCoachMarks: () => false,
}));
vi.mock("./MessageInput", () => ({ submitSuggestion: mocks.submitSuggestion }));

import { resetActivitySuggestionsForTests } from "../models/ActivitySuggestions";
import type { ChatSettings } from "../models/ChatSettings";
import { DEFAULT_CHAT_SETTINGS, resetChatSettingsForTests } from "../models/ChatSettings";
import { chatSnapshotFixture, provisionalChatFixture } from "../models/chatSnapshotFixture";
import { addOutgoing, clearOutgoing, getOutgoingMessages } from "../models/OutgoingMessages";
import { ownerAndVisitorRosterFixture } from "../models/participantFixture";
import { setParticipantsForTesting } from "../models/Participants";
import { announceMessageSent, type TranscriptEvent } from "../models/Response";
import { resetSuggestionsForTests, type SuggestionsAnswer } from "../models/Suggestions";
import { assistantMessageFixture, userMessageFixture } from "../models/transcriptEventFixture";
import { isTurnDoneForSuggestions, lastReplyEventId } from "./last-reply";
import { ChatSuggestions } from "./ChatSuggestions";
import { SUGGESTIONS_OFF_NOTICE_MS } from "./suggestion-placement";

const CHAT = "agent-first-run";
const SEED_END = `${CHAT}:seed:3`;

function reply(eventId: string): TranscriptEvent {
  return assistantMessageFixture(eventId, "**How do you want to start?**");
}

function userMessage(eventId: string, display?: "skill_expansion"): TranscriptEvent {
  return userMessageFixture(eventId, "See what Studio can do", display);
}

const START_SET: SuggestionsAnswer = {
  status: "ready",
  after_event_id: SEED_END,
  source: "fixed",
  suggestions: [
    { suggestion_id: "start-describe", label: "Describe what you want to make", is_recommended: false },
    { suggestion_id: "start-ideas", label: "Get ideas for what to make", is_recommended: false },
    { suggestion_id: "start-features", label: "See what Studio can do", is_recommended: false },
  ],
};

const START_LABELS = START_SET.suggestions.map((suggestion) => suggestion.label);

/** What the backend holds, which the page's requests read and write. */
let stored: { settings: ChatSettings; suggestions: SuggestionsAnswer; isWriteRefused: boolean };
/** While a list, each settings write waits here once the backend has taken it, until the test lets it answer. */
let heldWriteAnswers: (() => void)[] | null;
let requests: { method: string; url: string; body?: unknown }[];
let root: HTMLElement;

/** The strips above the composer, of which these tests offer the next-step one alone: the chat app answers the
 *  other placements with nothing. */
function draw(): void {
  m.render(root, m(ChatSuggestions, { chatId: CHAT }));
}

/** Let the page's requests answer, then draw what they brought. */
async function settle(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  draw();
}

/** A page that has loaded: the first draw asks for the settings and the stored set, the next shows them. */
async function load(): Promise<void> {
  draw();
  await settle();
}

function pillLabels(): string[] {
  return [...root.querySelectorAll(".suggestion-pill")].map((pill) => pill.textContent ?? "");
}

function click(selector: string): void {
  const element = root.querySelector<HTMLElement>(selector);
  expect(element, `${selector} should be drawn`).not.toBeNull();
  element!.click();
  draw();
}

/** A seeded first-run chat that still awaits its first agent: its welcome on the page, no chat listed. */
function seededAndAwaiting(): void {
  mocks.chat = undefined;
  mocks.provisional = provisionalChatFixture(CHAT, { phase: "awaiting_first_send", is_seeded: true, account_id: "" });
  mocks.events = [reply(`${CHAT}:seed:0`), reply(SEED_END)];
}

/** The same chat once its first agent is being created with nothing sent to it, as the chat app starts it when its
 *  owner leaves the first run's provider page. */
function firstAgentStarting(): void {
  mocks.chat = undefined;
  mocks.provisional = provisionalChatFixture(CHAT, { phase: "creating", is_seeded: true, message: "" });
  mocks.events = [reply(`${CHAT}:seed:0`), reply(SEED_END)];
}

/** The chat app saying, over the page's socket, that a chat's stored set was replaced or removed. */
function announceSuggestionsUpdated(chatId: string): void {
  for (const listener of mocks.suggestionsUpdatedListeners) listener(chatId);
}

function writes(): unknown[] {
  return requests.filter((request) => request.method === "PUT").map((request) => [request.url, request.body]);
}

beforeEach(() => {
  resetSuggestionsForTests();
  resetActivitySuggestionsForTests();
  resetChatSettingsForTests();
  setParticipantsForTesting("owner", []);
  stored = { settings: { ...DEFAULT_CHAT_SETTINGS }, suggestions: START_SET, isWriteRefused: false };
  heldWriteAnswers = null;
  requests = [];
  root = document.createElement("div");
  mocks.isLaunchInFlight = false;
  mocks.isRoom = false;
  mocks.hasMoreAfter = false;
  mocks.isTranscriptLoaded = true;
  mocks.opensWorkspaceSettings = false;
  mocks.submitSuggestion.mockClear();
  mocks.openWorkspaceSettings.mockClear();
  seededAndAwaiting();
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  vi.spyOn(m, "request").mockImplementation((async (options: { method: string; url: string; body?: unknown }) => {
    requests.push({ method: options.method, url: options.url, body: options.body });
    if (options.url === "/api/settings") return { settings: stored.settings };
    if (options.url === "/api/suggestions/activity") return { suggestions: [] };
    if (options.url === "/api/settings/suggestions") {
      if (stored.isWriteRefused) throw new Error("400");
      stored.settings = { ...stored.settings, ...(options.body as Partial<ChatSettings>) };
      const answer = { settings: stored.settings };
      const held = heldWriteAnswers;
      if (held !== null) await new Promise<void>((release) => held.push(release));
      return answer;
    }
    return stored.suggestions;
  }) as unknown as typeof m.request);
});

afterEach(() => {
  m.render(root, null);
  clearOutgoing(
    CHAT,
    getOutgoingMessages(CHAT).map((message) => message.id),
  );
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("whether the chat's turn is done, for its suggestions", () => {
  it("counts a seeded chat awaiting its first send as done, until something is sent or its launch starts", () => {
    expect(isTurnDoneForSuggestions(CHAT)).toBe(true);

    mocks.isLaunchInFlight = true;
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);
    mocks.isLaunchInFlight = false;

    addOutgoing(CHAT, "See what Studio can do");
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);
  });

  it("counts a chat whose first agent is starting with nothing sent to it as done", () => {
    firstAgentStarting();
    expect(isTurnDoneForSuggestions(CHAT)).toBe(true);

    // A page that cannot tell yet whether anybody has spoken does not take it that nobody has.
    mocks.isTranscriptLoaded = false;
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);
  });

  it("does not count a starting chat a message is on its way to", () => {
    // The launch brought the first message, as another page's send of it does.
    mocks.provisional = provisionalChatFixture(CHAT, {
      phase: "creating",
      is_seeded: true,
      message: "See what Studio can do",
    });
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);

    // A message sent while the agent starts is held for it.
    mocks.provisional = provisionalChatFixture(CHAT, {
      phase: "creating",
      is_seeded: true,
      held_sends: [{ message_id: "m-1", text: "See what Studio can do" }],
    });
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);

    firstAgentStarting();
    mocks.isLaunchInFlight = true;
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);
    mocks.isLaunchInFlight = false;

    addOutgoing(CHAT, "See what Studio can do");
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);
  });

  it("does not count a starting chat somebody has spoken in, nor one whose create failed", () => {
    firstAgentStarting();
    mocks.events = [reply(SEED_END), userMessage("u-1")];
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);

    firstAgentStarting();
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "failed", is_seeded: true, error: "no such account" });
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);
  });

  it("reads a listed chat as the done mark does", () => {
    mocks.provisional = undefined;
    mocks.chat = chatSnapshotFixture(CHAT, { active_agent: { activity_state: "IDLE" } });
    expect(isTurnDoneForSuggestions(CHAT)).toBe(true);

    mocks.chat = chatSnapshotFixture(CHAT, { status: "working", active_agent: { activity_state: "THINKING" } });
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);

    mocks.chat = chatSnapshotFixture(CHAT, { status: "attention", active_agent: { activity_state: "IDLE" } });
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);
  });

  it("does not count a listed chat whose agent is working, though nobody has spoken in it on this page", () => {
    // A chat an agent started: its first message is not one this page holds, and its agent is mid-turn.
    mocks.provisional = undefined;
    mocks.events = [reply(SEED_END)];
    mocks.chat = chatSnapshotFixture(CHAT, { status: "working", active_agent: { activity_state: "TOOL_RUNNING" } });
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);
  });

  it("does not count a chat the page knows nothing of", () => {
    mocks.provisional = undefined;
    expect(isTurnDoneForSuggestions(CHAT)).toBe(false);
  });
});

describe("the reply a set of suggestions may follow", () => {
  it("is the last assistant message the page holds", () => {
    expect(lastReplyEventId(CHAT)).toBe(SEED_END);
  });

  it("is none once somebody has said something after it", () => {
    mocks.events = [reply(SEED_END), userMessage("u-1")];
    expect(lastReplyEventId(CHAT)).toBeNull();

    mocks.events = [reply(SEED_END), userMessage("u-1"), reply("a-2")];
    expect(lastReplyEventId(CHAT)).toBe("a-2");
  });

  it("looks past a message that opens no turn", () => {
    mocks.events = [reply(SEED_END), userMessage("u-skill", "skill_expansion")];
    expect(lastReplyEventId(CHAT)).toBe(SEED_END);
  });

  it("is none with no transcript, or with a window that stops short of the transcript's end", () => {
    mocks.events = [];
    expect(lastReplyEventId(CHAT)).toBeNull();

    mocks.events = [reply(SEED_END)];
    mocks.hasMoreAfter = true;
    expect(lastReplyEventId(CHAT)).toBeNull();
  });
});

describe("the next-step strip", () => {
  it("offers the start suggestions in a seeded chat that still awaits its first agent", async () => {
    await load();

    // The last is what a chat nobody has spoken in asks for besides, and here it answers nothing.
    expect(requests.map((request) => `${request.method} ${request.url}`).sort()).toEqual([
      "GET /api/chats/:chatId/suggestions",
      "GET /api/settings",
      "GET /api/suggestions/activity",
    ]);
    expect(pillLabels()).toEqual(START_LABELS);
    expect(root.querySelector(".suggestion-strip")?.getAttribute("data-placement")).toBe("next_step");
  });

  it("keeps the start suggestions open while the chat's first agent starts with nothing sent to it", async () => {
    await load();
    expect(pillLabels()).toEqual(START_LABELS);

    firstAgentStarting();
    draw();
    expect(pillLabels()).toEqual(START_LABELS);
    expect(root.querySelector(".suggestion-strip-toggle")).toBeNull();

    // The agent is up, idle, and still has been sent nothing.
    mocks.provisional = undefined;
    mocks.chat = chatSnapshotFixture(CHAT, { active_agent: { activity_state: "IDLE" } });
    await settle();

    expect(pillLabels()).toEqual(START_LABELS);
    // No turn ended, so the set was read once, when the page first looked.
    expect(requests.filter((request) => request.url === "/api/chats/:chatId/suggestions")).toHaveLength(1);
  });

  it("draws a generated set's first three with Show more for the rest, and a fixed set whole", async () => {
    const five = ["One", "Two", "Three", "Four", "Five"].map((label) => ({
      suggestion_id: label.toLowerCase(),
      label,
      is_recommended: false,
    }));
    stored.suggestions = { ...START_SET, source: "generated", suggestions: five };
    await load();

    expect(pillLabels()).toEqual(["One", "Two", "Three"]);
    click(".suggestion-strip-more");
    expect(pillLabels()).toEqual(["One", "Two", "Three", "Four", "Five"]);

    resetSuggestionsForTests();
    m.render(root, null);
    stored.suggestions = { ...START_SET, source: "fixed", suggestions: five };
    await load();

    expect(pillLabels()).toEqual(["One", "Two", "Three", "Four", "Five"]);
    expect(root.querySelector(".suggestion-strip-more")).toBeNull();
  });

  it("draws a set the chat app says was stored after the page had read none", async () => {
    stored.suggestions = { status: "pending", after_event_id: SEED_END, source: null, suggestions: [] };
    await load();
    expect(pillLabels()).toEqual([]);

    stored.suggestions = START_SET;
    announceSuggestionsUpdated("agent-some-other-chat");
    await settle();
    expect(pillLabels()).toEqual([]);

    announceSuggestionsUpdated(CHAT);
    await settle();

    expect(pillLabels()).toEqual(START_LABELS);

    m.render(root, null);
    expect(mocks.suggestionsUpdatedListeners).toEqual([]);
  });

  it("sends a clicked suggestion's label through the composer", async () => {
    await load();

    click(".suggestion-pill:nth-child(2)");

    expect(mocks.submitSuggestion).toHaveBeenCalledTimes(1);
    expect(mocks.submitSuggestion).toHaveBeenCalledWith(CHAT, "Get ideas for what to make");
  });

  it("collapses to the Next steps toggle when a message is sent, and opens again with its pills still clickable", async () => {
    await load();

    announceMessageSent(CHAT);
    draw();
    expect(pillLabels()).toEqual([]);
    expect(root.querySelector(".suggestion-strip-toggle")?.textContent).toBe("Next steps");

    click(".suggestion-strip-toggle");
    expect(pillLabels()).toEqual(START_LABELS);
    click(".suggestion-pill");
    expect(mocks.submitSuggestion).toHaveBeenCalledWith(CHAT, "Describe what you want to make");
  });

  it("stays open when a message is sent in some other chat", async () => {
    await load();

    announceMessageSent("agent-elsewhere");
    draw();

    expect(pillLabels()).toHaveLength(3);
  });

  it("goes when the turn the send opened ends, and shows nothing under a reply the stored set is not for", async () => {
    await load();
    addOutgoing(CHAT, "See what Studio can do");
    draw();
    expect(root.querySelector(".suggestion-strip-toggle")).not.toBeNull();

    // The chat's agent is up and has answered; the start set is still the one stored.
    clearOutgoing(
      CHAT,
      getOutgoingMessages(CHAT).map((message) => message.id),
    );
    mocks.provisional = undefined;
    mocks.chat = chatSnapshotFixture(CHAT, { active_agent: { activity_state: "IDLE" } });
    mocks.events = [reply(SEED_END), userMessage("u-1"), reply("a-2")];
    draw();
    await settle();

    expect(root.querySelector(".suggestion-strip")).toBeNull();
    expect(
      requests.filter((request) => request.url.endsWith("/suggestions") && request.method === "GET"),
    ).toHaveLength(2);
  });

  it("shows nothing after a reload of a chat whose stored set is for an earlier reply", async () => {
    mocks.provisional = undefined;
    mocks.chat = chatSnapshotFixture(CHAT, { active_agent: { activity_state: "IDLE" } });
    mocks.events = [reply(SEED_END), userMessage("u-1"), reply("a-2")];

    await load();

    expect(root.querySelector(".suggestion-strip")).toBeNull();
  });

  it("shows nothing in a room, or with next-step suggestions switched off", async () => {
    mocks.isRoom = true;
    await load();
    expect(root.querySelector(".suggestion-strip")).toBeNull();

    mocks.isRoom = false;
    stored.settings = { ...stored.settings, is_next_step_suggestions_enabled: false };
    resetSuggestionsForTests();
    resetChatSettingsForTests();
    await load();
    expect(root.querySelector(".suggestion-strip")).toBeNull();
  });

  it("shows the strip to a page whose backend sends no suggestion switches", async () => {
    // DEFAULT_CHAT_SETTINGS names none of the switches, as an older backend's answer does.
    expect(stored.settings.is_next_step_suggestions_enabled).toBeUndefined();

    await load();

    expect(pillLabels()).toHaveLength(3);
  });
});

describe("the link that switches next-step suggestions off", () => {
  it("is shown to the workspace's owner only", async () => {
    await load();
    expect(root.querySelector(".suggestion-strip-disable")?.textContent).toBe("disable next step suggestions");

    setParticipantsForTesting("user:owner-1", ownerAndVisitorRosterFixture());
    draw();
    expect(root.querySelector(".suggestion-strip-disable")).not.toBeNull();

    setParticipantsForTesting("user:glenn-2", ownerAndVisitorRosterFixture());
    draw();
    expect(root.querySelector(".suggestion-strip-disable")).toBeNull();
    expect(pillLabels()).toHaveLength(3);

    // Before the page knows who its viewer is, it offers the link to no one.
    setParticipantsForTesting(null, []);
    draw();
    expect(root.querySelector(".suggestion-strip-disable")).toBeNull();
  });

  it("switches the placement off alone, and puts a line with Undo in the strip's place", async () => {
    await load();

    click(".suggestion-strip-disable");

    expect(writes()).toEqual([["/api/settings/suggestions", { is_next_step_suggestions_enabled: false }]]);
    expect(root.querySelector(".suggestion-strip")).toBeNull();
    expect(root.querySelector(".suggestions-off-notice span")?.textContent).toBe("Next step suggestions are off.");
    expect(root.querySelector(".suggestions-off-undo")?.textContent).toBe("Undo");
    // No chrome said it opens workspace settings, so nothing is asked of one.
    expect(mocks.openWorkspaceSettings).not.toHaveBeenCalled();

    await settle();
    expect(root.querySelector(".suggestions-off-notice")).not.toBeNull();
    expect(root.querySelector(".suggestion-strip")).toBeNull();
  });

  it("switches it back on from Undo, and the strip returns", async () => {
    await load();
    click(".suggestion-strip-disable");
    await settle();

    click(".suggestions-off-undo");

    expect(writes()).toEqual([
      ["/api/settings/suggestions", { is_next_step_suggestions_enabled: false }],
      ["/api/settings/suggestions", { is_next_step_suggestions_enabled: true }],
    ]);
    expect(root.querySelector(".suggestions-off-notice")).toBeNull();
    await settle();
    expect(pillLabels()).toEqual(START_LABELS);
  });

  it("takes the line away after about eight seconds, leaving nothing", async () => {
    vi.useFakeTimers();
    await load();
    click(".suggestion-strip-disable");
    await settle();

    vi.advanceTimersByTime(SUGGESTIONS_OFF_NOTICE_MS - 1);
    draw();
    expect(root.querySelector(".suggestions-off-notice")).not.toBeNull();

    vi.advanceTimersByTime(1);
    draw();
    expect(root.querySelector(".suggestions-off-notice")).toBeNull();
    expect(root.querySelector(".suggestion-strip")).toBeNull();
    expect(SUGGESTIONS_OFF_NOTICE_MS).toBe(8_000);
  });

  it("also opens the chrome's workspace settings on their Suggestions group, where a chrome said it does", async () => {
    mocks.opensWorkspaceSettings = true;
    await load();

    click(".suggestion-strip-disable");

    // The settings read the switch as they open, so they are asked for only once the write has answered.
    expect(mocks.openWorkspaceSettings).not.toHaveBeenCalled();
    await settle();

    expect(mocks.openWorkspaceSettings).toHaveBeenCalledTimes(1);
    expect(mocks.openWorkspaceSettings).toHaveBeenCalledWith("suggestions");
    expect(writes()).toEqual([["/api/settings/suggestions", { is_next_step_suggestions_enabled: false }]]);
  });

  it("opens no settings when Undo is clicked before the write has answered", async () => {
    mocks.opensWorkspaceSettings = true;
    await load();
    heldWriteAnswers = [];

    click(".suggestion-strip-disable");
    click(".suggestions-off-undo");
    expect(heldWriteAnswers).toHaveLength(2);
    heldWriteAnswers[0]();
    await settle();
    heldWriteAnswers[1]();
    await settle();

    expect(mocks.openWorkspaceSettings).not.toHaveBeenCalled();
    expect(writes()).toEqual([
      ["/api/settings/suggestions", { is_next_step_suggestions_enabled: false }],
      ["/api/settings/suggestions", { is_next_step_suggestions_enabled: true }],
    ]);
    expect(pillLabels()).toHaveLength(3);
  });

  it("puts the strip back, with no line and no settings opened, when the backend refuses the write", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mocks.opensWorkspaceSettings = true;
    stored.isWriteRefused = true;
    await load();

    click(".suggestion-strip-disable");
    expect(root.querySelector(".suggestions-off-notice")).not.toBeNull();
    await settle();

    expect(root.querySelector(".suggestions-off-notice")).toBeNull();
    expect(pillLabels()).toHaveLength(3);
    expect(mocks.openWorkspaceSettings).not.toHaveBeenCalled();
  });
});
