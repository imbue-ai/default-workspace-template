// @vitest-environment jsdom
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// vi.mock factories are hoisted above module scope, so what they close over comes from vi.hoisted.
const mocks = vi.hoisted(() => ({
  chat: undefined as unknown,
  isRoom: false,
  events: [] as unknown[],
  submitSuggestion: vi.fn(),
}));

vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));
vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: () => mocks.chat,
  getProvisionalChat: () => undefined,
  isLaunchInFlight: () => false,
  isRoomChat: () => mocks.isRoom,
}));
vi.mock("../models/Response", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Response")>()),
  getEventsForChat: () => mocks.events,
  hasMoreAfter: () => false,
  hasMoreBefore: () => false,
  isTranscriptLoaded: () => true,
}));
vi.mock("../shell", () => ({
  opensWorkspaceSettings: () => false,
  openWorkspaceSettings: () => undefined,
  showsCoachMarks: () => false,
}));
vi.mock("./MessageInput", () => ({ submitSuggestion: mocks.submitSuggestion }));

import type { ChatSettings } from "../models/ChatSettings";
import { DEFAULT_CHAT_SETTINGS, resetChatSettingsForTests } from "../models/ChatSettings";
import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import { resetNewChatSuggestionsForTests } from "../models/NewChatSuggestions";
import { addOutgoing, clearOutgoing, getOutgoingMessages } from "../models/OutgoingMessages";
import { setParticipantsForTesting } from "../models/Participants";
import type { TranscriptEvent } from "../models/Response";
import { resetSuggestionsForTests, type SuggestionsAnswer } from "../models/Suggestions";
import {
  storedSuggestionsAnswerFixture as remembering,
  suggestionSetFixture,
} from "../models/suggestionsAnswerFixture";
import { assistantMessageFixture, userMessageFixture } from "../models/transcriptEventFixture";
import { answeredReplySuggestions } from "./AnsweredReplySuggestions";
import { ChatSuggestions } from "./ChatSuggestions";

const CHAT = "agent-chat";

function reply(eventId: string): TranscriptEvent {
  return assistantMessageFixture(eventId, "Here is the plan.");
}

function userMessage(eventId: string): TranscriptEvent {
  return userMessageFixture(eventId, "Plan my week");
}

const FIRST = suggestionSetFixture("a-1", ["Go ahead", "Change the plan"], "generated");
const SECOND = suggestionSetFixture("a-2", ["Thanks, that is all"], "generated");

/** What the backend holds, which the page's requests read. */
let stored: { settings: ChatSettings; suggestions: SuggestionsAnswer };
/** How many times the page has read the chat's sets. */
let setReadCount: number;
let composerRoot: HTMLElement;
let transcriptRoot: HTMLElement;

/** Draw the page's two halves in the order the chat panel does: the transcript's rows, then the composer's strips. */
function draw(): void {
  const eventIds = (mocks.events as TranscriptEvent[])
    .filter((event) => event.type === "assistant_message")
    .map((event) => event.event_id);
  m.render(
    transcriptRoot,
    eventIds.map((eventId) =>
      m("div", { key: eventId, "data-reply": eventId }, answeredReplySuggestions(CHAT, [eventId])),
    ),
  );
  m.render(composerRoot, m(ChatSuggestions, { chatId: CHAT }));
}

/** Let the page's requests answer, then draw what they brought. */
async function settle(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  draw();
}

async function load(): Promise<void> {
  draw();
  await settle();
  await settle();
}

/** The line in the transcript under the reply ``eventId``, or null when it has none. */
function lineUnder(eventId: string): HTMLElement | null {
  return transcriptRoot.querySelector<HTMLElement>(`[data-reply="${eventId}"] .answered-reply-suggestions`);
}

function pillsUnder(eventId: string): HTMLButtonElement[] {
  return [...(lineUnder(eventId)?.querySelectorAll<HTMLButtonElement>(".suggestion-pill") ?? [])];
}

function toggleUnder(eventId: string): HTMLButtonElement {
  const toggle = lineUnder(eventId)?.querySelector<HTMLButtonElement>(".suggestion-line-toggle");
  expect(toggle, `the reply ${eventId} should keep its line`).toBeDefined();
  return toggle!;
}

function composerPillLabels(): string[] {
  return [...composerRoot.querySelectorAll(".suggestion-pill")].map((pill) => pill.textContent ?? "");
}

/** The agent has answered once and is idle: the reply a-1 is the one the chat waits on. */
function answeredOnce(): void {
  mocks.chat = chatSnapshotFixture(CHAT, { active_agent: { activity_state: "IDLE" } });
  mocks.events = [userMessage("u-1"), reply("a-1")];
}

/** A second message is in and the agent has answered it: a-1 has been answered, and a-2 is waited on. */
function answeredTwice(): void {
  mocks.chat = chatSnapshotFixture(CHAT, { active_agent: { activity_state: "IDLE" } });
  mocks.events = [userMessage("u-1"), reply("a-1"), userMessage("u-2"), reply("a-2")];
}

beforeEach(() => {
  resetSuggestionsForTests();
  resetNewChatSuggestionsForTests();
  resetChatSettingsForTests();
  setParticipantsForTesting("owner", []);
  stored = { settings: { ...DEFAULT_CHAT_SETTINGS }, suggestions: remembering([FIRST]) };
  setReadCount = 0;
  composerRoot = document.createElement("div");
  transcriptRoot = document.createElement("div");
  mocks.isRoom = false;
  mocks.submitSuggestion.mockClear();
  answeredOnce();
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  vi.spyOn(m, "request").mockImplementation((async (options: { url: string }) => {
    if (options.url === "/api/settings") return { settings: stored.settings };
    if (options.url === "/api/suggestions/new-chat") return { suggestions: [] };
    setReadCount++;
    return stored.suggestions;
  }) as unknown as typeof m.request);
});

afterEach(() => {
  m.render(composerRoot, null);
  m.render(transcriptRoot, null);
  clearOutgoing(
    CHAT,
    getOutgoingMessages(CHAT).map((message) => message.id),
  );
  vi.restoreAllMocks();
});

describe("the suggestions of the reply the chat waits on", () => {
  it("are above the composer and not in the transcript", async () => {
    await load();

    expect(composerRoot.querySelector(".suggestion-line-toggle")?.textContent).toBe("See 2 suggestions");
    expect(lineUnder("a-1")).toBeNull();
  });
});

describe("the suggestions of a reply that has been answered", () => {
  it("move to the transcript under their reply the moment a message is sent, with nothing read again", async () => {
    await load();
    expect(setReadCount).toBe(1);

    addOutgoing(CHAT, "Go ahead");
    draw();

    expect(toggleUnder("a-1").textContent).toBe("See 2 suggestions");
    expect(composerRoot.querySelector(".suggestion-line-toggle")).toBeNull();
    expect(composerRoot.querySelector(".suggestion-line")?.textContent).toBe("");
    expect(setReadCount).toBe(1);
  });

  it("start hidden there though they were shown above the composer when the message was sent", async () => {
    await load();
    composerRoot.querySelector<HTMLElement>(".suggestion-line-toggle")!.click();
    draw();
    expect(composerPillLabels()).toEqual(["Go ahead", "Change the plan"]);

    addOutgoing(CHAT, "Go ahead");
    draw();

    expect(toggleUnder("a-1").getAttribute("aria-expanded")).toBe("false");
    expect(pillsUnder("a-1")).toEqual([]);
    expect(composerPillLabels()).toEqual([]);
  });

  it("show their pills under the line when asked, and hide them again", async () => {
    await load();
    addOutgoing(CHAT, "Go ahead");
    draw();

    toggleUnder("a-1").click();
    draw();

    const toggle = toggleUnder("a-1");
    expect(toggle.textContent).toBe("Hide suggestions");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(pillsUnder("a-1").map((pill) => pill.textContent)).toEqual(["Go ahead", "Change the plan"]);
    expect(toggle.compareDocumentPosition(pillsUnder("a-1")[0]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The transcript's line is no place to switch the placement off.
    expect(lineUnder("a-1")?.querySelector(".suggestion-strip-disable")).toBeNull();

    toggleUnder("a-1").click();
    draw();

    expect(pillsUnder("a-1")).toEqual([]);
    expect(toggleUnder("a-1").textContent).toBe("See 2 suggestions");
  });

  it("send a clicked pill's label through the composer, and keep their line", async () => {
    stored.suggestions = remembering([FIRST, SECOND]);
    answeredTwice();
    await load();
    toggleUnder("a-1").click();
    draw();

    pillsUnder("a-1")[1].click();
    draw();

    expect(mocks.submitSuggestion).toHaveBeenCalledExactlyOnceWith(CHAT, "Change the plan");
    expect(toggleUnder("a-1").textContent).toBe("Hide suggestions");
  });

  it("keep one line under every answered reply the page knows a set for, and none under the reply waited on", async () => {
    stored.suggestions = remembering([FIRST, SECOND]);
    answeredTwice();
    await load();

    expect(toggleUnder("a-1").textContent).toBe("See 2 suggestions");
    expect(lineUnder("a-2")).toBeNull();
    expect(composerRoot.querySelector(".suggestion-line-toggle")?.textContent).toBe("See 1 suggestion");

    // The second reply is answered too: both keep their line, each opened by itself.
    mocks.events = [...(mocks.events as TranscriptEvent[]), userMessage("u-3")];
    mocks.chat = chatSnapshotFixture(CHAT, { status: "working", active_agent: { activity_state: "THINKING" } });
    draw();
    toggleUnder("a-2").click();
    draw();

    expect(transcriptRoot.querySelectorAll(".answered-reply-suggestions")).toHaveLength(2);
    expect(pillsUnder("a-2").map((pill) => pill.textContent)).toEqual(["Thanks, that is all"]);
    expect(pillsUnder("a-1")).toEqual([]);
  });

  it("draw the line of the last of a part's replies that has one", async () => {
    stored.suggestions = remembering([FIRST, SECOND]);
    mocks.chat = chatSnapshotFixture(CHAT, { status: "working", active_agent: { activity_state: "THINKING" } });
    mocks.events = [userMessage("u-1"), reply("a-1"), reply("a-2"), userMessage("u-2")];
    await load();
    const root = document.createElement("div");

    m.render(root, answeredReplySuggestions(CHAT, ["a-0", "a-1", "a-2", "a-3"]));

    expect(root.querySelectorAll(".answered-reply-suggestions")).toHaveLength(1);
    expect(root.querySelector(".answered-reply-suggestions")?.getAttribute("data-after-event-id")).toBe("a-2");
    m.render(root, null);
  });

  it("are not drawn in a room, nor with next-step suggestions switched off", async () => {
    stored.suggestions = remembering([FIRST, SECOND]);
    answeredTwice();
    await load();
    expect(lineUnder("a-1")).not.toBeNull();

    mocks.isRoom = true;
    draw();
    expect(lineUnder("a-1")).toBeNull();

    mocks.isRoom = false;
    stored.settings = { ...stored.settings, is_next_step_suggestions_enabled: false };
    resetChatSettingsForTests();
    await load();
    expect(lineUnder("a-1")).toBeNull();
  });
});
