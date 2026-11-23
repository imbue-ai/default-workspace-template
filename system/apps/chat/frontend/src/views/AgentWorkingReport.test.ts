// @vitest-environment jsdom
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// vi.mock factories are hoisted above module scope, so what they close over comes from vi.hoisted.
const mocks = vi.hoisted(() => ({
  chat: undefined as unknown,
  provisional: undefined as unknown,
  isRoom: false,
  events: [] as unknown[],
  isTranscriptLoaded: true,
  reportAgentWorking: vi.fn((_isWorking: boolean) => undefined),
}));

vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));
vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: () => mocks.chat,
  getProvisionalChat: () => mocks.provisional,
  isLaunchInFlight: () => false,
  isRoomChat: () => mocks.isRoom,
}));
vi.mock("../models/Response", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Response")>()),
  getEventsForChat: () => mocks.events,
  hasMoreAfter: () => false,
  hasMoreBefore: () => false,
  isTranscriptLoaded: () => mocks.isTranscriptLoaded,
}));
vi.mock("../shell", () => ({
  opensWorkspaceSettings: () => false,
  openWorkspaceSettings: () => undefined,
  showsCoachMarks: () => true,
  openShareSettingsWithCoachMark: () => undefined,
  showCoachCard: () => undefined,
  reportAgentWorking: mocks.reportAgentWorking,
}));
vi.mock("./MessageInput", () => ({ submitSuggestion: () => undefined, focusComposer: () => true }));

import type { ChatSettings } from "../models/ChatSettings";
import { DEFAULT_CHAT_SETTINGS, resetChatSettingsForTests } from "../models/ChatSettings";
import { chatSnapshotFixture, provisionalChatFixture } from "../models/chatSnapshotFixture";
import { resetNewChatSuggestionsForTests } from "../models/NewChatSuggestions";
import { ALL_FOUR } from "../models/newChatSuggestionFixture";
import { addOutgoing, clearOutgoing, getOutgoingMessages } from "../models/OutgoingMessages";
import { setParticipantsForTesting } from "../models/Participants";
import type { TranscriptEvent } from "../models/Response";
import { resetSuggestionsForTests } from "../models/Suggestions";
import { NO_SUGGESTIONS_ANSWER } from "../models/suggestionsAnswerFixture";
import { assistantMessageFixture, userMessageFixture } from "../models/transcriptEventFixture";
import { AgentWorkingReport, resetAgentWorkingReportForTests } from "./AgentWorkingReport";
import { ChatSuggestions } from "./ChatSuggestions";
import { isTurnDoneForWorkingReport } from "./last-reply";

const CHAT = "agent-chat";

let root: HTMLElement;
let settings: ChatSettings;

function reply(eventId: string): TranscriptEvent {
  return assistantMessageFixture(eventId, "Here is the plan.");
}

function userMessage(eventId: string): TranscriptEvent {
  return userMessageFixture(eventId, "Plan my week");
}

function draw(): void {
  m.render(root, m(AgentWorkingReport, { chatId: CHAT }));
}

/** The footer of a chat page, as far as the report and the suggestions go. */
function drawFooter(): void {
  m.render(root, [m(AgentWorkingReport, { chatId: CHAT }), m(ChatSuggestions, { chatId: CHAT })]);
}

/** Draw the footer, let the page's requests answer, then draw what they brought. */
async function drawFooterAndSettle(): Promise<void> {
  drawFooter();
  for (let i = 0; i < 20; i++) await Promise.resolve();
  drawFooter();
}

/** What the shell was told at the last look: the page tells it on every look, and only a change is passed on
 *  (``reportAgentWorking``). */
function lastTold(): boolean | undefined {
  const calls = mocks.reportAgentWorking.mock.calls;
  return calls.length === 0 ? undefined : calls[calls.length - 1][0];
}

/** A chat somebody has spoken in whose agent has answered: listed, idle, its transcript on the page. */
function answered(): void {
  mocks.provisional = undefined;
  mocks.chat = chatSnapshotFixture(CHAT, { active_agent: { activity_state: "IDLE" } });
  mocks.events = [userMessage("u-1"), reply("a-1")];
}

/** The same chat while its agent works on a reply to a second message. */
function working(): void {
  mocks.chat = chatSnapshotFixture(CHAT, { status: "working", active_agent: { activity_state: "THINKING" } });
  mocks.events = [userMessage("u-1"), reply("a-1"), userMessage("u-2")];
}

/** The reply to the second message is in. */
function answeredAgain(): void {
  mocks.chat = chatSnapshotFixture(CHAT, { active_agent: { activity_state: "IDLE" } });
  mocks.events = [userMessage("u-1"), reply("a-1"), userMessage("u-2"), reply("a-2")];
}

/** The chat with its agent dead: stopped, crashed, or not started again since the workspace restarted. */
function agentStopped(agent: { is_connecting?: boolean } = {}): void {
  mocks.chat = chatSnapshotFixture(CHAT, {
    status: "stopped",
    active_agent: { state: "STOPPED", activity_state: "IDLE", ...agent },
  });
}

/** A chat started from the New chat button: listed, idle, and nobody has said anything in it. */
function newAndEmpty(): void {
  mocks.provisional = undefined;
  mocks.chat = chatSnapshotFixture(CHAT, { active_agent: { activity_state: "IDLE" } });
  mocks.events = [];
}

function clearOutgoingMessages(): void {
  clearOutgoing(
    CHAT,
    getOutgoingMessages(CHAT).map((message) => message.id),
  );
}

beforeEach(() => {
  resetAgentWorkingReportForTests();
  resetSuggestionsForTests();
  resetNewChatSuggestionsForTests();
  resetChatSettingsForTests();
  setParticipantsForTesting("owner", []);
  settings = { ...DEFAULT_CHAT_SETTINGS };
  root = document.createElement("div");
  document.body.appendChild(root);
  mocks.isRoom = false;
  mocks.isTranscriptLoaded = true;
  mocks.reportAgentWorking.mockClear();
  answered();
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  vi.spyOn(m, "request").mockImplementation((async (options: { url: string }) => {
    if (options.url === "/api/settings") return { settings };
    if (options.url === "/api/suggestions/new-chat") return ALL_FOUR;
    return NO_SUGGESTIONS_ANSWER;
  }) as unknown as typeof m.request);
});

afterEach(() => {
  m.render(root, null);
  root.remove();
  clearOutgoingMessages();
  vi.restoreAllMocks();
});

describe("whether a reply is being worked on in a chat", () => {
  it("cannot be told before the transcript has loaded, nor of a chat the page has not heard of", () => {
    newAndEmpty();
    mocks.isTranscriptLoaded = false;
    expect(isTurnDoneForWorkingReport(CHAT)).toBeNull();

    mocks.isTranscriptLoaded = true;
    mocks.chat = undefined;
    mocks.provisional = undefined;
    expect(isTurnDoneForWorkingReport(CHAT)).toBeNull();
  });

  it("is no in a chat nobody has spoken in, though its agent is still starting", () => {
    mocks.events = [];
    mocks.chat = undefined;
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "creating" });
    expect(isTurnDoneForWorkingReport(CHAT)).toBe(true);

    answered();
    expect(isTurnDoneForWorkingReport(CHAT)).toBe(true);
    working();
    expect(isTurnDoneForWorkingReport(CHAT)).toBe(false);
  });

  it("is no where nobody is left to work on one, until a message is on its way", () => {
    agentStopped();
    expect(isTurnDoneForWorkingReport(CHAT)).toBe(true);
    // The chat's switch to another agent failed.
    mocks.chat = chatSnapshotFixture(CHAT, { status: "error", active_agent: { activity_state: "IDLE" } });
    expect(isTurnDoneForWorkingReport(CHAT)).toBe(true);

    // A message wakes a stopped chat: one this page is sending, or one the chat app is starting the agent for.
    agentStopped();
    addOutgoing(CHAT, "Carry on");
    expect(isTurnDoneForWorkingReport(CHAT)).toBe(false);
    clearOutgoingMessages();
    agentStopped({ is_connecting: true });
    expect(isTurnDoneForWorkingReport(CHAT)).toBe(false);

    // A chat whose create failed holds its first message, and nobody to answer it.
    const heldSends = [{ message_id: "m-1", text: "Plan my week" }];
    mocks.chat = undefined;
    mocks.events = [];
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "failed", held_sends: heldSends });
    expect(isTurnDoneForWorkingReport(CHAT)).toBe(true);
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "creating", held_sends: heldSends });
    expect(isTurnDoneForWorkingReport(CHAT)).toBe(false);
  });
});

describe("what the page tells the shell of a reply under way", () => {
  it("draws nothing", () => {
    working();
    draw();

    expect(root.childNodes).toHaveLength(0);
  });

  it("says an agent is working from a turn's start to its end", () => {
    draw();
    expect(lastTold()).toBe(false);

    working();
    draw();
    expect(lastTold()).toBe(true);

    answeredAgain();
    draw();
    expect(lastTold()).toBe(false);
  });

  it("says so for a message still on its way", () => {
    draw();
    expect(lastTold()).toBe(false);

    addOutgoing(CHAT, "And on Thursdays?");
    draw();

    expect(lastTold()).toBe(true);
  });

  it("says nobody is working while a first agent starts with nothing sent to it", () => {
    mocks.events = [];
    mocks.chat = undefined;
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "creating" });
    draw();

    expect(mocks.reportAgentWorking).toHaveBeenCalled();
    expect(mocks.reportAgentWorking.mock.calls.every(([isWorking]) => isWorking === false)).toBe(true);
  });

  it("keeps what it last said while it cannot tell, and says nothing is under way in a room", () => {
    working();
    draw();
    expect(lastTold()).toBe(true);

    // The chat list has not come back yet (a socket reconnecting): the page knows neither way.
    mocks.chat = undefined;
    draw();
    expect(lastTold()).toBe(true);

    working();
    mocks.isRoom = true;
    draw();
    expect(lastTold()).toBe(false);
  });

  it("takes it back when the page stops showing the chat", () => {
    working();
    draw();
    expect(lastTold()).toBe(true);

    m.render(root, null);

    expect(lastTold()).toBe(false);
  });

  it("says so beside suggestions that draw no strip at all: both placements off, in a chat with messages", async () => {
    settings = {
      ...DEFAULT_CHAT_SETTINGS,
      is_next_step_suggestions_enabled: false,
      is_new_chat_suggestions_enabled: false,
    };
    await drawFooterAndSettle();
    expect(lastTold()).toBe(false);

    working();
    drawFooter();

    expect(lastTold()).toBe(true);
    expect(root.querySelector(".suggestion-strip")).toBeNull();
    expect(root.querySelector(".suggestion-line")).toBeNull();

    answeredAgain();
    drawFooter();
    expect(lastTold()).toBe(false);
  });

  it("says so on a new chat's first message, with new chat suggestions switched off", async () => {
    settings = { ...DEFAULT_CHAT_SETTINGS, is_new_chat_suggestions_enabled: false };
    newAndEmpty();
    await drawFooterAndSettle();
    expect(lastTold()).toBe(false);
    expect(root.querySelector(".suggestion-strip")).toBeNull();

    mocks.chat = chatSnapshotFixture(CHAT, { status: "working", active_agent: { activity_state: "THINKING" } });
    mocks.events = [userMessage("u-1")];
    drawFooter();

    expect(lastTold()).toBe(true);
    expect(root.querySelector(".suggestion-strip")).toBeNull();
  });
});
