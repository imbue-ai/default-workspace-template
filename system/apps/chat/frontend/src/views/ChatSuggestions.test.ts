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
  submitSuggestion: vi.fn(),
  focusComposer: vi.fn(() => true),
  opensWorkspaceSettings: false,
  openWorkspaceSettings: vi.fn(),
  showsCoachMarks: true,
  openShareSettingsWithCoachMark: vi.fn(),
  showCoachCard: vi.fn(),
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
  opensWorkspaceSettings: () => mocks.opensWorkspaceSettings,
  openWorkspaceSettings: mocks.openWorkspaceSettings,
  showsCoachMarks: () => mocks.showsCoachMarks,
  openShareSettingsWithCoachMark: mocks.openShareSettingsWithCoachMark,
  showCoachCard: mocks.showCoachCard,
  reportAgentWorking: mocks.reportAgentWorking,
}));
vi.mock("./MessageInput", () => ({
  submitSuggestion: mocks.submitSuggestion,
  focusComposer: mocks.focusComposer,
}));

import { resetActivitySuggestionsForTests } from "../models/ActivitySuggestions";
import type { ActionPlacement, ActivityAnswer } from "../models/ActivitySuggestions";
import { ALL_FOUR, BUILD_APP, CONNECT_DATA, NEW_CHAT, PHONE } from "../models/activitySuggestionFixture";
import type { ChatSettings } from "../models/ChatSettings";
import { DEFAULT_CHAT_SETTINGS, resetChatSettingsForTests } from "../models/ChatSettings";
import { chatSnapshotFixture, provisionalChatFixture } from "../models/chatSnapshotFixture";
import { addOutgoing, clearOutgoing, getOutgoingMessages } from "../models/OutgoingMessages";
import { ownerAndVisitorRosterFixture } from "../models/participantFixture";
import { setParticipantsForTesting } from "../models/Participants";
import { announceMessageSent, type TranscriptEvent } from "../models/Response";
import { resetSuggestionsForTests, type SuggestionsAnswer } from "../models/Suggestions";
import { assistantMessageFixture, userMessageFixture } from "../models/transcriptEventFixture";
import { ChatSuggestions } from "./ChatSuggestions";
import { hasUserMessage, isTurnDoneForActivity } from "./last-reply";
import { SUGGESTIONS_OFF_NOTICE_MS } from "./suggestion-placement";

const CHAT = "agent-chat";

const FIRST_THREE_LABELS = [NEW_CHAT.label, PHONE.label, BUILD_APP.label];

function reply(eventId: string): TranscriptEvent {
  return assistantMessageFixture(eventId, "Here is the plan.");
}

function userMessage(eventId: string): TranscriptEvent {
  return userMessageFixture(eventId, "Plan my week");
}

const NO_NEXT_STEPS: SuggestionsAnswer = { status: "none", after_event_id: null, source: null, suggestions: [] };

function nextSteps(afterEventId: string): SuggestionsAnswer {
  return {
    status: "ready",
    after_event_id: afterEventId,
    source: "generated",
    suggestions: [
      { suggestion_id: "go", label: "Go ahead", is_recommended: false },
      { suggestion_id: "change", label: "Change the plan", is_recommended: false },
    ],
  };
}

/** What the backend holds, which the page's requests read and write. */
let stored: {
  settings: ChatSettings;
  nextSteps: SuggestionsAnswer;
  offered: Record<ActionPlacement, ActivityAnswer>;
};
let requests: { method: string; url: string; params?: unknown; body?: unknown }[];
let root: HTMLElement;
let showNewChatCoachMark: ReturnType<typeof vi.fn>;

function draw(): void {
  m.render(root, m(ChatSuggestions, { chatId: CHAT }));
}

/** Let the page's requests answer, then draw what they brought. */
async function settle(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  draw();
}

/** Draw, and let what the draw asked for answer: the settings on a page's first draw, then what the draw that
 *  has them asks the chat app for. */
async function drawAndSettle(): Promise<void> {
  draw();
  await settle();
  await settle();
}

function strips(): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(".suggestion-strip")];
}

function strip(placement: string): HTMLElement | null {
  return root.querySelector<HTMLElement>(`.suggestion-strip[data-placement="${placement}"]`);
}

function isOpen(placement: string): boolean | null {
  return strip(placement)?.classList.contains("suggestion-strip--open") ?? null;
}

function pillLabels(placement: string): string[] {
  return [...(strip(placement)?.querySelectorAll(".suggestion-pill") ?? [])].map((pill) => pill.textContent ?? "");
}

function click(selector: string): void {
  const element = root.querySelector<HTMLElement>(selector);
  expect(element, `${selector} should be drawn`).not.toBeNull();
  element!.click();
  draw();
}

function clickPill(placement: string, label: string): void {
  const pill = [...(strip(placement)?.querySelectorAll<HTMLElement>(".suggestion-pill") ?? [])].find(
    (candidate) => candidate.textContent === label,
  );
  expect(pill, `${label} should be offered`).toBeDefined();
  pill!.click();
  draw();
}

function writes(): unknown[] {
  return requests.filter((request) => request.method === "PUT").map((request) => [request.url, request.body]);
}

function readsOf(placement: ActionPlacement): number {
  return requests.filter(
    (request) =>
      request.url === "/api/suggestions/activity" && (request.params as { placement: string }).placement === placement,
  ).length;
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

/** The first message of the new chat is in, and its agent is working on the reply. */
function firstMessageSent(): void {
  mocks.chat = chatSnapshotFixture(CHAT, { status: "working", active_agent: { activity_state: "THINKING" } });
  mocks.events = [userMessage("u-1")];
}

function firstReplyIn(): void {
  mocks.chat = chatSnapshotFixture(CHAT, { active_agent: { activity_state: "IDLE" } });
  mocks.events = [userMessage("u-1"), reply("a-1")];
}

beforeEach(() => {
  resetSuggestionsForTests();
  resetActivitySuggestionsForTests();
  resetChatSettingsForTests();
  setParticipantsForTesting("owner", []);
  stored = {
    settings: { ...DEFAULT_CHAT_SETTINGS },
    nextSteps: NO_NEXT_STEPS,
    offered: { activity: ALL_FOUR, new_chat: ALL_FOUR },
  };
  requests = [];
  root = document.createElement("div");
  document.body.appendChild(root);
  mocks.isRoom = false;
  mocks.isTranscriptLoaded = true;
  mocks.opensWorkspaceSettings = false;
  mocks.showsCoachMarks = true;
  mocks.submitSuggestion.mockClear();
  mocks.focusComposer.mockClear();
  mocks.openWorkspaceSettings.mockClear();
  mocks.openShareSettingsWithCoachMark.mockClear();
  mocks.showCoachCard.mockClear();
  mocks.reportAgentWorking.mockClear();
  // The chat list frames the page, and draws the coach mark at its New chat button.
  showNewChatCoachMark = vi.fn();
  Object.defineProperty(window, "parent", {
    value: { chatRootEmbed: { chooseProviderForFirstSend: () => undefined, showNewChatCoachMark } },
    configurable: true,
  });
  answered();
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  vi.spyOn(m, "request").mockImplementation((async (options: {
    method: string;
    url: string;
    params?: unknown;
    body?: unknown;
  }) => {
    requests.push({ method: options.method, url: options.url, params: options.params, body: options.body });
    if (options.url === "/api/settings") return { settings: stored.settings };
    if (options.url === "/api/settings/suggestions") {
      stored.settings = { ...stored.settings, ...(options.body as Partial<ChatSettings>) };
      return { settings: stored.settings };
    }
    if (options.url === "/api/suggestions/activity") {
      return stored.offered[(options.params as { placement: ActionPlacement }).placement];
    }
    if (options.url === "/api/suggestions/activity/clicked") return {};
    return stored.nextSteps;
  }) as unknown as typeof m.request);
});

afterEach(() => {
  m.render(root, null);
  root.remove();
  document.querySelector<HTMLElement>(".coach-mark-dismiss")?.click();
  document.body.replaceChildren();
  clearOutgoing(
    CHAT,
    getOutgoingMessages(CHAT).map((message) => message.id),
  );
  Object.defineProperty(window, "parent", { value: window, configurable: true });
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("what the page reads of a chat for its activity and new-chat strips", () => {
  it("counts a message of the transcript, one on its way, and one held for a chat still being created", () => {
    expect(hasUserMessage(CHAT)).toBe(true);

    newAndEmpty();
    expect(hasUserMessage(CHAT)).toBe(false);

    addOutgoing(CHAT, "Plan my week");
    expect(hasUserMessage(CHAT)).toBe(true);
    clearOutgoing(
      CHAT,
      getOutgoingMessages(CHAT).map((message) => message.id),
    );

    mocks.chat = undefined;
    mocks.provisional = provisionalChatFixture(CHAT, {
      phase: "creating",
      held_sends: [{ message_id: "m-1", text: "Plan my week" }],
    });
    expect(hasUserMessage(CHAT)).toBe(true);

    // The message a chat's create or launch brought, which a page that did not send it holds nowhere else.
    mocks.events = [];
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "creating", message: "Plan my week" });
    expect(hasUserMessage(CHAT)).toBe(true);
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "creating", message: "" });
    expect(hasUserMessage(CHAT)).toBe(false);
  });

  it("cannot tell before the transcript has loaded, except for a chat still awaiting its first send", () => {
    newAndEmpty();
    mocks.isTranscriptLoaded = false;
    expect(hasUserMessage(CHAT)).toBeNull();
    expect(isTurnDoneForActivity(CHAT)).toBeNull();

    mocks.chat = undefined;
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "awaiting_first_send", account_id: "" });
    expect(hasUserMessage(CHAT)).toBe(false);
  });

  it("takes no reply to be under way in a chat nobody has spoken in, though its agent is still starting", () => {
    mocks.events = [];
    mocks.chat = undefined;
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "creating" });
    expect(isTurnDoneForActivity(CHAT)).toBe(true);

    answered();
    expect(isTurnDoneForActivity(CHAT)).toBe(true);
    working();
    expect(isTurnDoneForActivity(CHAT)).toBe(false);

    // A chat the page has not heard of yet is not one whose turn it can read.
    mocks.chat = undefined;
    mocks.provisional = undefined;
    expect(isTurnDoneForActivity(CHAT)).toBeNull();
  });

  it("takes no reply to be under way where nobody is left to work on one, until a message is on its way", () => {
    agentStopped();
    expect(isTurnDoneForActivity(CHAT)).toBe(true);
    // The chat's switch to another agent failed.
    mocks.chat = chatSnapshotFixture(CHAT, { status: "error", active_agent: { activity_state: "IDLE" } });
    expect(isTurnDoneForActivity(CHAT)).toBe(true);

    // A message wakes a stopped chat: one this page is sending, or one the chat app is starting the agent for.
    agentStopped();
    addOutgoing(CHAT, "Carry on");
    expect(isTurnDoneForActivity(CHAT)).toBe(false);
    clearOutgoing(
      CHAT,
      getOutgoingMessages(CHAT).map((message) => message.id),
    );
    agentStopped({ is_connecting: true });
    expect(isTurnDoneForActivity(CHAT)).toBe(false);

    // A chat whose create failed holds its first message, and nobody to answer it.
    const heldSends = [{ message_id: "m-1", text: "Plan my week" }];
    mocks.chat = undefined;
    mocks.events = [];
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "failed", held_sends: heldSends });
    expect(isTurnDoneForActivity(CHAT)).toBe(true);
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "creating", held_sends: heldSends });
    expect(isTurnDoneForActivity(CHAT)).toBe(false);
  });
});

describe("what the page tells the shell of a reply under way", () => {
  /** What the shell was told at the last look: the page tells it on every look, and only a change is passed on
   *  (``reportAgentWorking``). */
  function lastTold(): boolean | undefined {
    const calls = mocks.reportAgentWorking.mock.calls;
    return calls.length === 0 ? undefined : calls[calls.length - 1][0];
  }

  it("says an agent is working from a turn's start to its end", async () => {
    await drawAndSettle();
    expect(lastTold()).toBe(false);

    working();
    draw();
    expect(lastTold()).toBe(true);

    answeredAgain();
    draw();
    expect(lastTold()).toBe(false);
  });

  it("says so for a message still on its way, and with the activity strip switched off", async () => {
    stored.settings = { ...DEFAULT_CHAT_SETTINGS, is_activity_suggestions_enabled: false };
    await drawAndSettle();
    expect(lastTold()).toBe(false);

    addOutgoing(CHAT, "And on Thursdays?");
    draw();

    expect(lastTold()).toBe(true);
    expect(strip("activity")).toBeNull();
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
});

describe("the activity strip", () => {
  it("is absent while the chat waits on its user", async () => {
    await drawAndSettle();

    expect(strips()).toEqual([]);
    expect(root.querySelector(".chat-suggestions")).toBeNull();
    expect(readsOf("activity")).toBe(0);
  });

  it("opens above the composer when a turn starts, with the first three things worth trying", async () => {
    await drawAndSettle();
    working();
    await drawAndSettle();

    expect(readsOf("activity")).toBe(1);
    expect(isOpen("activity")).toBe(true);
    expect(strip("activity")?.getAttribute("aria-label")).toBe("While you wait");
    expect(pillLabels("activity")).toEqual(FIRST_THREE_LABELS);
    expect(root.querySelector(".suggestion-strip-more")).toBeNull();
  });

  it("closes into its While you wait toggle when the reply ends, and the reply's own suggestions open", async () => {
    await drawAndSettle();
    working();
    await drawAndSettle();

    stored.nextSteps = nextSteps("a-2");
    answeredAgain();
    await drawAndSettle();

    expect(isOpen("activity")).toBe(false);
    expect(strip("activity")?.querySelector(".suggestion-strip-toggle")?.textContent).toBe("While you wait");
    expect(isOpen("next_step")).toBe(true);
    expect(pillLabels("next_step")).toEqual(["Go ahead", "Change the plan"]);
    expect(root.querySelectorAll(".suggestion-strip--open")).toHaveLength(1);
  });

  it("opens again from its toggle with every pill still clickable, and the open strip gives way", async () => {
    await drawAndSettle();
    working();
    await drawAndSettle();
    stored.nextSteps = nextSteps("a-2");
    answeredAgain();
    await drawAndSettle();

    click('.suggestion-strip[data-placement="activity"] .suggestion-strip-toggle');

    expect(isOpen("activity")).toBe(true);
    expect(isOpen("next_step")).toBe(false);
    expect(root.querySelectorAll(".suggestion-strip--open")).toHaveLength(1);
    const textbox = document.createElement("textarea");
    textbox.className = "message-input-textbox";
    document.body.appendChild(textbox);
    clickPill("activity", BUILD_APP.label);
    expect(document.querySelector(".coach-mark .coach-mark-text")?.textContent).toBe(
      "Provide some context on a problem you're facing and tell the agent to build an app to help with it.",
    );
    expect(mocks.focusComposer).toHaveBeenCalledTimes(1);

    // And back: opening the reply's suggestions collapses the activity strip.
    click('.suggestion-strip[data-placement="next_step"] .suggestion-strip-toggle');
    expect(isOpen("next_step")).toBe(true);
    expect(isOpen("activity")).toBe(false);
  });

  it("collapses the reply's suggestions, already a toggle, no further when the next turn opens it", async () => {
    stored.nextSteps = nextSteps("a-1");
    await drawAndSettle();
    expect(isOpen("next_step")).toBe(true);

    working();
    await drawAndSettle();

    expect(isOpen("activity")).toBe(true);
    expect(isOpen("next_step")).toBe(false);
  });

  it("skips what this page cannot act on before taking the first three", async () => {
    // No chat list frames the page, and no chrome draws coach marks: only the composer is within reach.
    Object.defineProperty(window, "parent", { value: window, configurable: true });
    mocks.showsCoachMarks = false;
    working();
    await drawAndSettle();

    expect(pillLabels("activity")).toEqual([BUILD_APP.label]);

    // A chrome that draws coach marks, still with no chat list: the fourth suggestion moves up.
    mocks.showsCoachMarks = true;
    draw();
    expect(pillLabels("activity")).toEqual([PHONE.label, BUILD_APP.label, CONNECT_DATA.label]);
  });

  it("is absent when the chat app offers nothing: a visitor, an unfinished tour, the first-run tree", async () => {
    stored.offered.activity = { suggestions: [] };
    working();
    await drawAndSettle();

    expect(strip("activity")).toBeNull();
  });

  it("is never drawn in a room", async () => {
    mocks.isRoom = true;
    working();
    await drawAndSettle();

    expect(strips()).toEqual([]);
    expect(requests.filter((request) => request.url === "/api/suggestions/activity")).toEqual([]);
  });

  it("is absent on a chat whose agent is stopped, and closes into its toggle when the agent stops mid-reply", async () => {
    agentStopped();
    await drawAndSettle();

    expect(strips()).toEqual([]);
    expect(readsOf("activity")).toBe(0);

    working();
    await drawAndSettle();
    expect(isOpen("activity")).toBe(true);

    agentStopped();
    await drawAndSettle();
    expect(isOpen("activity")).toBe(false);
  });
});

describe("the new-chat strip", () => {
  it("is open as soon as a chat nobody has spoken in is on screen", async () => {
    newAndEmpty();
    await drawAndSettle();

    expect(readsOf("new_chat")).toBe(1);
    expect(isOpen("new_chat")).toBe(true);
    expect(strip("new_chat")?.getAttribute("aria-label")).toBe("Things to try");
    expect(pillLabels("new_chat")).toEqual(FIRST_THREE_LABELS);
    expect(strip("activity")).toBeNull();
  });

  it("is shown on a chat still awaiting its first send, which has no transcript to load", async () => {
    mocks.chat = undefined;
    mocks.events = [];
    mocks.isTranscriptLoaded = false;
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "awaiting_first_send", account_id: "" });
    await drawAndSettle();

    expect(isOpen("new_chat")).toBe(true);
  });

  it("is absent with an empty answer, as in the first-run chat, and on a chat that already has a message", async () => {
    newAndEmpty();
    stored.offered.new_chat = { suggestions: [] };
    await drawAndSettle();
    expect(strip("new_chat")).toBeNull();

    resetActivitySuggestionsForTests();
    stored.offered.new_chat = stored.offered.activity;
    answered();
    await drawAndSettle();
    expect(strip("new_chat")).toBeNull();
    expect(readsOf("new_chat")).toBe(1);
  });

  it("is dropped on the first send when the activity strip opens with the same suggestions", async () => {
    newAndEmpty();
    await drawAndSettle();

    firstMessageSent();
    draw();
    // The activity strip's read is out: the two are not on screen together even for a draw.
    expect(strips()).toEqual([]);
    await settle();

    expect(isOpen("activity")).toBe(true);
    expect(strip("new_chat")).toBeNull();
    expect(strips()).toHaveLength(1);

    firstReplyIn();
    await drawAndSettle();
    expect(strip("new_chat")).toBeNull();
    expect(isOpen("activity")).toBe(false);
  });

  it("collapses to its Things to try toggle on the first send when the activity strip differs", async () => {
    newAndEmpty();
    await drawAndSettle();
    stored.offered.activity = { suggestions: [PHONE, BUILD_APP, CONNECT_DATA] };

    firstMessageSent();
    await drawAndSettle();

    expect(isOpen("new_chat")).toBe(false);
    expect(strip("new_chat")?.querySelector(".suggestion-strip-toggle")?.textContent).toBe("Things to try");
    expect(isOpen("activity")).toBe(true);
    expect(pillLabels("activity")).toEqual([PHONE.label, BUILD_APP.label, CONNECT_DATA.label]);
  });

  it("collapses on a send of this page's before the chat app has heard of it", async () => {
    newAndEmpty();
    stored.offered.activity = { suggestions: [BUILD_APP] };
    await drawAndSettle();

    addOutgoing(CHAT, "Plan my week");
    await drawAndSettle();

    expect(isOpen("new_chat")).toBe(false);
    expect(pillLabels("activity")).toEqual([BUILD_APP.label]);
  });

  it("collapses again on a later send when opened from its toggle, with no activity strip to give way to", async () => {
    stored.settings = { ...stored.settings, is_activity_suggestions_enabled: false };
    newAndEmpty();
    await drawAndSettle();
    firstMessageSent();
    await drawAndSettle();
    firstReplyIn();
    await drawAndSettle();
    click('.suggestion-strip[data-placement="new_chat"] .suggestion-strip-toggle');
    expect(isOpen("new_chat")).toBe(true);

    announceMessageSent("agent-elsewhere");
    draw();
    expect(isOpen("new_chat")).toBe(true);

    announceMessageSent(CHAT);
    working();
    draw();
    expect(isOpen("new_chat")).toBe(false);
    expect(strip("activity")).toBeNull();
  });
});

describe("the strips together", () => {
  /** A new chat whose first reply is in, with different things offered while waiting and on a new chat: all three
   *  placements have a strip. */
  async function allThreeStrips(): Promise<void> {
    newAndEmpty();
    await drawAndSettle();
    stored.offered.activity = { suggestions: [PHONE, BUILD_APP, CONNECT_DATA] };
    firstMessageSent();
    await drawAndSettle();
    stored.nextSteps = nextSteps("a-1");
    firstReplyIn();
    await drawAndSettle();
  }

  it("share one column, the collapsed ones as toggles side by side and the open one in a row of its own", async () => {
    await allThreeStrips();

    const column = root.querySelector<HTMLElement>(".chat-suggestions");
    expect(column).not.toBeNull();
    expect(strips().every((drawn) => drawn.parentElement === column)).toBe(true);
    expect(column!.classList.contains("flex-wrap")).toBe(true);
    expect(strips().map((drawn) => drawn.getAttribute("data-placement"))).toEqual([
      "next_step",
      "activity",
      "new_chat",
    ]);
    expect([isOpen("next_step"), isOpen("activity"), isOpen("new_chat")]).toEqual([true, false, false]);
    // The open strip takes a whole row, after the toggles, which keep their own width.
    expect(strip("next_step")!.classList.contains("w-full")).toBe(true);
    expect(strip("next_step")!.classList.contains("order-last")).toBe(true);
    for (const placement of ["activity", "new_chat"]) {
      expect(strip(placement)!.classList.contains("w-full")).toBe(false);
      expect(strip(placement)!.querySelector(".suggestion-strip-toggle")).not.toBeNull();
    }
  });

  it("keep one open at a time, whichever toggle is clicked", async () => {
    await allThreeStrips();

    click('.suggestion-strip[data-placement="new_chat"] .suggestion-strip-toggle');
    expect([isOpen("next_step"), isOpen("activity"), isOpen("new_chat")]).toEqual([false, false, true]);
    expect(pillLabels("new_chat")).toEqual(FIRST_THREE_LABELS);

    click('.suggestion-strip[data-placement="activity"] .suggestion-strip-toggle');
    expect([isOpen("next_step"), isOpen("activity"), isOpen("new_chat")]).toEqual([false, true, false]);

    // Nothing reopens the ones that gave way: they stay toggles through later draws.
    draw();
    expect(root.querySelectorAll(".suggestion-strip--open")).toHaveLength(1);
  });
});

describe("a click on an action suggestion", () => {
  beforeEach(async () => {
    stored.offered.activity = { suggestions: [NEW_CHAT, PHONE, CONNECT_DATA, BUILD_APP] };
    working();
    await drawAndSettle();
    requests = [];
  });

  it("sends nothing, and asks the chat list for the coach mark at its New chat button", async () => {
    clickPill("activity", NEW_CHAT.label);

    expect(showNewChatCoachMark).toHaveBeenCalledTimes(1);
    expect(mocks.submitSuggestion).not.toHaveBeenCalled();
  });

  it("asks the chrome for its share panel with a coach mark at phone access", () => {
    clickPill("activity", PHONE.label);

    expect(mocks.openShareSettingsWithCoachMark).toHaveBeenCalledWith("mobile-access");
  });

  it("asks the shell for a card under the chrome's permissions button", () => {
    clickPill("activity", CONNECT_DATA.label);

    expect(mocks.showCoachCard).toHaveBeenCalledWith(
      "Your connections live here. Ask me to connect any of these services, or any other data you want to pull in.",
      "permissions",
    );
  });

  it("is recorded, and what the chat app offers is read again, so a retired suggestion gives its place up", async () => {
    stored.offered.activity = { suggestions: [NEW_CHAT, PHONE, BUILD_APP] };

    clickPill("activity", CONNECT_DATA.label);
    await settle();

    expect(requests).toEqual([
      {
        method: "POST",
        url: "/api/suggestions/activity/clicked",
        params: undefined,
        body: { suggestion_id: "connect_data" },
      },
      {
        method: "GET",
        url: "/api/suggestions/activity",
        params: { placement: "activity", chat_id: CHAT },
        body: undefined,
      },
    ]);
    expect(pillLabels("activity")).toEqual(FIRST_THREE_LABELS);
  });
});

describe("the link that switches a placement off", () => {
  it("switches activity suggestions off alone, with a line and Undo in the strip's place", async () => {
    working();
    await drawAndSettle();
    expect(strip("activity")?.querySelector(".suggestion-strip-disable")?.textContent).toBe(
      "disable activity suggestions",
    );

    click('.suggestion-strip[data-placement="activity"] .suggestion-strip-disable');

    expect(writes()).toEqual([["/api/settings/suggestions", { is_activity_suggestions_enabled: false }]]);
    expect(strip("activity")).toBeNull();
    const notice = root.querySelector('.suggestions-off-notice[data-placement="activity"]');
    expect(notice?.querySelector("span")?.textContent).toBe("Activity suggestions are off.");
    expect(mocks.openWorkspaceSettings).not.toHaveBeenCalled();

    await settle();
    click(".suggestions-off-undo");

    expect(writes()).toEqual([
      ["/api/settings/suggestions", { is_activity_suggestions_enabled: false }],
      ["/api/settings/suggestions", { is_activity_suggestions_enabled: true }],
    ]);
    expect(root.querySelector(".suggestions-off-notice")).toBeNull();
    await settle();
    expect(pillLabels("activity")).toEqual(FIRST_THREE_LABELS);
  });

  it("switches new chat suggestions off alone, and Undo brings the strip back", async () => {
    newAndEmpty();
    await drawAndSettle();
    expect(strip("new_chat")?.querySelector(".suggestion-strip-disable")?.textContent).toBe(
      "disable new chat suggestions",
    );

    click('.suggestion-strip[data-placement="new_chat"] .suggestion-strip-disable');

    expect(writes()).toEqual([["/api/settings/suggestions", { is_new_chat_suggestions_enabled: false }]]);
    expect(strip("new_chat")).toBeNull();
    expect(root.querySelector('.suggestions-off-notice[data-placement="new_chat"] span')?.textContent).toBe(
      "New chat suggestions are off.",
    );

    await settle();
    click(".suggestions-off-undo");
    await settle();

    expect(isOpen("new_chat")).toBe(true);
    expect(pillLabels("new_chat")).toEqual(FIRST_THREE_LABELS);
  });

  it("takes the line away after about eight seconds, and leaves the other placements' strips alone", async () => {
    vi.useFakeTimers();
    working();
    await drawAndSettle();
    stored.nextSteps = nextSteps("a-2");
    answeredAgain();
    await drawAndSettle();
    click('.suggestion-strip[data-placement="activity"] .suggestion-strip-toggle');

    click('.suggestion-strip[data-placement="activity"] .suggestion-strip-disable');
    await settle();
    expect(root.querySelector(".suggestions-off-notice")).not.toBeNull();
    expect(strip("next_step")).not.toBeNull();

    vi.advanceTimersByTime(SUGGESTIONS_OFF_NOTICE_MS);
    draw();

    expect(root.querySelector(".suggestions-off-notice")).toBeNull();
    expect(strip("activity")).toBeNull();
    expect(strip("next_step")).not.toBeNull();
  });

  it("opens the chrome's workspace settings on their Suggestions group, where a chrome said it does", async () => {
    mocks.opensWorkspaceSettings = true;
    working();
    await drawAndSettle();

    click('.suggestion-strip[data-placement="activity"] .suggestion-strip-disable');
    expect(mocks.openWorkspaceSettings).not.toHaveBeenCalled();
    await settle();

    expect(mocks.openWorkspaceSettings).toHaveBeenCalledTimes(1);
    expect(mocks.openWorkspaceSettings).toHaveBeenCalledWith("suggestions");
  });

  it("is offered to the workspace's owner only", async () => {
    setParticipantsForTesting("user:glenn-2", ownerAndVisitorRosterFixture());
    working();
    await drawAndSettle();

    expect(pillLabels("activity")).toEqual(FIRST_THREE_LABELS);
    expect(root.querySelector(".suggestion-strip-disable")).toBeNull();
  });

  it("removes a strip at once when its placement is switched off somewhere else", async () => {
    working();
    await drawAndSettle();
    expect(isOpen("activity")).toBe(true);

    // What the page holds once it has read the settings again after a settings_updated push.
    stored.settings = { ...stored.settings, is_activity_suggestions_enabled: false };
    resetChatSettingsForTests();
    await drawAndSettle();

    expect(strip("activity")).toBeNull();
    expect(root.querySelector(".suggestions-off-notice")).toBeNull();
  });
});
