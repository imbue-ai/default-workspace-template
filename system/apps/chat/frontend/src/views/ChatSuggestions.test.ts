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
}));
vi.mock("./MessageInput", () => ({
  submitSuggestion: mocks.submitSuggestion,
  focusComposer: mocks.focusComposer,
}));

import type { ChatSettings } from "../models/ChatSettings";
import { DEFAULT_CHAT_SETTINGS, resetChatSettingsForTests } from "../models/ChatSettings";
import { chatSnapshotFixture, provisionalChatFixture } from "../models/chatSnapshotFixture";
import { resetNewChatSuggestionsForTests } from "../models/NewChatSuggestions";
import type { NewChatAnswer } from "../models/NewChatSuggestions";
import { ALL_FOUR, BUILD_APP, CONNECT_DATA, NEW_CHAT, PHONE } from "../models/newChatSuggestionFixture";
import { addOutgoing, clearOutgoing, getOutgoingMessages } from "../models/OutgoingMessages";
import { ownerAndVisitorRosterFixture } from "../models/participantFixture";
import { setParticipantsForTesting } from "../models/Participants";
import type { TranscriptEvent } from "../models/Response";
import { resetSuggestionsForTests, type SuggestionsAnswer } from "../models/Suggestions";
import {
  NO_SUGGESTIONS_ANSWER as NO_NEXT_STEPS,
  storedSuggestionsAnswerFixture,
} from "../models/suggestionsAnswerFixture";
import { assistantMessageFixture, userMessageFixture } from "../models/transcriptEventFixture";
import { ChatSuggestions } from "./ChatSuggestions";
import { SuggestionLayer } from "./SuggestionLayer";
import { hasUserMessage } from "./last-reply";
import { SUGGESTIONS_OFF_NOTICE_MS } from "./suggestion-placement";

const CHAT = "agent-chat";

const FIRST_THREE_LABELS = [NEW_CHAT.label, PHONE.label, BUILD_APP.label];

function reply(eventId: string): TranscriptEvent {
  return assistantMessageFixture(eventId, "Here is the plan.");
}

function userMessage(eventId: string): TranscriptEvent {
  return userMessageFixture(eventId, "Plan my week");
}

/** A set stored for the reply ``afterEventId``: a model's, which waits behind its line, unless ``source`` says the
 *  chat app's own, which starts shown. */
function nextSteps(afterEventId: string, source: "generated" | "fixed" = "generated"): SuggestionsAnswer {
  return storedSuggestionsAnswerFixture([
    {
      after_event_id: afterEventId,
      source,
      suggestions: [
        { suggestion_id: "go", label: "Go ahead", is_recommended: false },
        { suggestion_id: "change", label: "Change the plan", is_recommended: false },
      ],
    },
  ]);
}

/** What the backend holds, which the page's requests read and write. */
let stored: {
  settings: ChatSettings;
  nextSteps: SuggestionsAnswer;
  offered: NewChatAnswer;
};
let requests: { method: string; url: string; params?: unknown; body?: unknown }[];
let root: HTMLElement;
let showNewChatCoachMark: ReturnType<typeof vi.fn>;

/** A strip's two parts, in the order the chat panel draws them: its open pills in the layer over the transcript,
 *  then its line above the composer. */
function draw(): void {
  m.render(root, [m(SuggestionLayer, { chatId: CHAT }), m(ChatSuggestions, { chatId: CHAT })]);
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

/** Whether the page draws nothing of any strip: no column above the composer, and nothing in the layer. */
function drawsNoStrip(): boolean {
  return (
    root.querySelector(".chat-suggestions") === null &&
    root.querySelector(".suggestion-layer")?.childElementCount === 0
  );
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

/** The open pills of a placement's strip, in the layer over the transcript. */
function layerStrip(placement: string): HTMLElement | null {
  return root.querySelector<HTMLElement>(`.suggestion-layer [data-placement="${placement}"]`);
}

function pillLabels(placement: string): string[] {
  return [...(layerStrip(placement)?.querySelectorAll(".suggestion-pill") ?? [])].map(
    (pill) => pill.textContent ?? "",
  );
}

/** What the next-step line above the composer says: "" for an empty one, null when its height is not kept. */
function nextStepLineText(): string | null {
  const drawn = root.querySelectorAll(".suggestion-line");
  expect(drawn.length).toBeLessThanOrEqual(1);
  return drawn[0]?.textContent ?? null;
}

function click(selector: string): void {
  const element = root.querySelector<HTMLElement>(selector);
  expect(element, `${selector} should be drawn`).not.toBeNull();
  element!.click();
  draw();
}

function clickPill(placement: string, label: string): void {
  const pill = [...(layerStrip(placement)?.querySelectorAll<HTMLElement>(".suggestion-pill") ?? [])].find(
    (candidate) => candidate.textContent === label,
  );
  expect(pill, `${label} should be offered`).toBeDefined();
  pill!.click();
  draw();
}

function writes(): unknown[] {
  return requests.filter((request) => request.method === "PUT").map((request) => [request.url, request.body]);
}

/** The reads of what the chat app offers a new chat, each as the chat it asked for. */
function newChatReads(): unknown[] {
  return requests.filter((request) => request.url === "/api/suggestions/new-chat").map((request) => request.params);
}

/** A chat somebody has spoken in whose agent has answered: listed, idle, its transcript on the page. */
function answered(): void {
  mocks.provisional = undefined;
  mocks.chat = chatSnapshotFixture(CHAT, { active_agent: { activity_state: "IDLE" } });
  mocks.events = [userMessage("u-1"), reply("a-1")];
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
  resetNewChatSuggestionsForTests();
  resetChatSettingsForTests();
  setParticipantsForTesting("owner", []);
  stored = { settings: { ...DEFAULT_CHAT_SETTINGS }, nextSteps: NO_NEXT_STEPS, offered: ALL_FOUR };
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
    if (options.url === "/api/suggestions/new-chat") return stored.offered;
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

describe("what the page reads of a chat for its new-chat strip", () => {
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

    mocks.chat = undefined;
    mocks.provisional = provisionalChatFixture(CHAT, { phase: "awaiting_first_send", account_id: "" });
    expect(hasUserMessage(CHAT)).toBe(false);
  });
});

describe("the new-chat strip", () => {
  it("is there as soon as a chat nobody has spoken in is on screen: its pills over the line with its link", async () => {
    newAndEmpty();
    await drawAndSettle();

    expect(newChatReads()).toEqual([{ chat_id: CHAT }]);
    expect(isOpen("new_chat")).toBe(true);
    expect(layerStrip("new_chat")?.getAttribute("aria-label")).toBe("Things to try");
    expect(pillLabels("new_chat")).toEqual(FIRST_THREE_LABELS);
    // Its line is the one quiet line above the composer, and holds its link alone.
    expect(nextStepLineText()).toBe("disable new chat suggestions");
    const line = root.querySelector<HTMLElement>(".suggestion-line")!;
    expect(line.parentElement).toBe(strip("new_chat"));
    expect([...line.children].map((child) => child.className.split(" ")[0])).toEqual(["suggestion-strip-disable"]);
    expect(root.querySelector(".chat-suggestions")?.children).toHaveLength(1);
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
    stored.offered = { suggestions: [] };
    await drawAndSettle();
    expect(strips()).toEqual([]);
    // The line above the composer keeps its height, empty.
    expect(nextStepLineText()).toBe("");
    expect(newChatReads()).toHaveLength(1);

    resetNewChatSuggestionsForTests();
    stored.offered = ALL_FOUR;
    answered();
    await drawAndSettle();
    expect(strips()).toEqual([]);
    expect(newChatReads()).toHaveLength(1);
  });

  it("skips what this page cannot act on before taking the first three", async () => {
    // No chat list frames the page, and no chrome draws coach marks: only the composer is within reach.
    Object.defineProperty(window, "parent", { value: window, configurable: true });
    mocks.showsCoachMarks = false;
    newAndEmpty();
    await drawAndSettle();

    expect(pillLabels("new_chat")).toEqual([BUILD_APP.label]);

    // A chrome that draws coach marks, still with no chat list: the fourth suggestion moves up.
    mocks.showsCoachMarks = true;
    draw();
    expect(pillLabels("new_chat")).toEqual([PHONE.label, BUILD_APP.label, CONNECT_DATA.label]);
  });

  it("is never drawn in a room", async () => {
    mocks.isRoom = true;
    newAndEmpty();
    await drawAndSettle();

    expect(drawsNoStrip()).toBe(true);
    expect(newChatReads()).toEqual([]);
  });

  it("is gone for good on the chat's first message, and leaves nothing behind to open it by", async () => {
    newAndEmpty();
    await drawAndSettle();
    expect(isOpen("new_chat")).toBe(true);

    firstMessageSent();
    await drawAndSettle();

    expect(strips()).toEqual([]);
    expect(root.querySelectorAll("button")).toHaveLength(0);
    // The line above the composer stands where the strip's line stood, empty while the reply is on its way.
    expect(nextStepLineText()).toBe("");
    expect(root.querySelector(".chat-suggestions")?.children).toHaveLength(1);

    stored.nextSteps = nextSteps("a-1");
    firstReplyIn();
    await drawAndSettle();

    expect(strip("new_chat")).toBeNull();
    expect(strips().map((drawn) => drawn.getAttribute("data-placement"))).toEqual(["next_step"]);
    expect(nextStepLineText()).toBe("See 2 suggestions");
    expect(newChatReads()).toHaveLength(1);
  });

  it("goes on a send of this page's before the chat app has heard of it", async () => {
    newAndEmpty();
    await drawAndSettle();

    addOutgoing(CHAT, "Plan my week");
    draw();
    expect(strips()).toEqual([]);

    // The send failed and was taken back: the chat has no message again, and the strip stays gone.
    clearOutgoing(
      CHAT,
      getOutgoingMessages(CHAT).map((message) => message.id),
    );
    await drawAndSettle();
    expect(strips()).toEqual([]);
  });

  it("gives way to the next-step strip should a chat nobody has spoken in have a reply with suggestions", async () => {
    newAndEmpty();
    mocks.events = [reply("a-1")];
    stored.nextSteps = nextSteps("a-1", "fixed");
    await drawAndSettle();

    expect(strips().map((drawn) => drawn.getAttribute("data-placement"))).toEqual(["next_step"]);
    expect(pillLabels("next_step")).toEqual(["Go ahead", "Change the plan"]);
    expect(root.querySelectorAll(".suggestion-line")).toHaveLength(1);
  });
});

describe("a click on a new-chat suggestion", () => {
  beforeEach(async () => {
    stored.offered = { suggestions: [NEW_CHAT, PHONE, CONNECT_DATA, BUILD_APP] };
    newAndEmpty();
    await drawAndSettle();
    requests = [];
  });

  it("sends nothing, and asks the chat list for the coach mark at its New chat button", async () => {
    clickPill("new_chat", NEW_CHAT.label);
    await settle();

    expect(showNewChatCoachMark).toHaveBeenCalledTimes(1);
    expect(mocks.submitSuggestion).not.toHaveBeenCalled();
    // The click is the page's own business: the chat app is told nothing and asked nothing.
    expect(requests).toEqual([]);
    expect(pillLabels("new_chat")).toEqual([NEW_CHAT.label, PHONE.label, CONNECT_DATA.label]);
  });

  it("asks the chrome for its share panel with a coach mark at phone access", () => {
    clickPill("new_chat", PHONE.label);

    expect(mocks.openShareSettingsWithCoachMark).toHaveBeenCalledWith("mobile-access");
  });

  it("asks the shell for a card under the chrome's permissions button, every time it is clicked", async () => {
    clickPill("new_chat", CONNECT_DATA.label);
    await settle();
    clickPill("new_chat", CONNECT_DATA.label);

    expect(mocks.showCoachCard).toHaveBeenCalledTimes(2);
    expect(mocks.showCoachCard).toHaveBeenLastCalledWith(
      "**Just ask your agent to connect anything!**\n\nYou can always see your connections here as well.",
      "permissions",
    );
    expect(requests).toEqual([]);
  });

  it("points a coach mark at the page's own composer for building an app", async () => {
    stored.offered = { suggestions: [BUILD_APP] };
    resetNewChatSuggestionsForTests();
    await drawAndSettle();
    const textbox = document.createElement("textarea");
    textbox.className = "message-input-textbox";
    document.body.appendChild(textbox);

    clickPill("new_chat", BUILD_APP.label);

    expect(document.querySelector(".coach-mark .coach-mark-text")?.textContent).toBe(
      "Provide some context on a problem you're facing and tell the agent to build an app to help with it.",
    );
    expect(mocks.focusComposer).toHaveBeenCalledTimes(1);
  });
});

describe("the link that switches a placement off", () => {
  it("switches new chat suggestions off alone, with a line and Undo in the strip's place", async () => {
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
    expect(mocks.openWorkspaceSettings).not.toHaveBeenCalled();
    // The line above the composer keeps its height under the notice.
    expect(nextStepLineText()).toBe("");

    await settle();
    click(".suggestions-off-undo");

    expect(writes()).toEqual([
      ["/api/settings/suggestions", { is_new_chat_suggestions_enabled: false }],
      ["/api/settings/suggestions", { is_new_chat_suggestions_enabled: true }],
    ]);
    expect(root.querySelector(".suggestions-off-notice")).toBeNull();
    await settle();
    expect(isOpen("new_chat")).toBe(true);
    expect(pillLabels("new_chat")).toEqual(FIRST_THREE_LABELS);
  });

  it("takes the line away after about eight seconds, and leaves the next-step line alone", async () => {
    vi.useFakeTimers();
    newAndEmpty();
    await drawAndSettle();

    click('.suggestion-strip[data-placement="new_chat"] .suggestion-strip-disable');
    await settle();
    expect(root.querySelector(".suggestions-off-notice")).not.toBeNull();

    vi.advanceTimersByTime(SUGGESTIONS_OFF_NOTICE_MS);
    draw();

    expect(root.querySelector(".suggestions-off-notice")).toBeNull();
    expect(strips()).toEqual([]);
    expect(nextStepLineText()).toBe("");
  });

  it("opens the chrome's workspace settings on their Suggestions group, where a chrome said it does", async () => {
    mocks.opensWorkspaceSettings = true;
    newAndEmpty();
    await drawAndSettle();

    click('.suggestion-strip[data-placement="new_chat"] .suggestion-strip-disable');
    expect(mocks.openWorkspaceSettings).not.toHaveBeenCalled();
    await settle();

    expect(mocks.openWorkspaceSettings).toHaveBeenCalledTimes(1);
    expect(mocks.openWorkspaceSettings).toHaveBeenCalledWith("suggestions");
  });

  it("is offered to the workspace's owner only", async () => {
    setParticipantsForTesting("user:glenn-2", ownerAndVisitorRosterFixture());
    newAndEmpty();
    await drawAndSettle();

    expect(pillLabels("new_chat")).toEqual(FIRST_THREE_LABELS);
    expect(root.querySelector(".suggestion-strip-disable")).toBeNull();
    expect(nextStepLineText()).toBe("");
  });

  it("removes a strip at once when its placement is switched off somewhere else", async () => {
    newAndEmpty();
    await drawAndSettle();
    expect(isOpen("new_chat")).toBe(true);

    // What the page holds once it has read the settings again after a settings_updated push.
    stored.settings = { ...stored.settings, is_new_chat_suggestions_enabled: false };
    resetChatSettingsForTests();
    await drawAndSettle();

    expect(strip("new_chat")).toBeNull();
    expect(root.querySelector(".suggestions-off-notice")).toBeNull();
  });
});

describe("where a strip's two parts are drawn", () => {
  let onElement: ReturnType<typeof vi.fn<(element: HTMLElement | null) => void>>;

  /** The chat panel's two homes for a strip: the transcript's scroll element, which the layer ends, and the footer. */
  function drawPage(): void {
    m.render(root, [
      m("main", m(SuggestionLayer, { chatId: CHAT, onElement })),
      m("footer", m(ChatSuggestions, { chatId: CHAT })),
    ]);
  }

  async function drawPageAndSettle(): Promise<void> {
    drawPage();
    for (let pass = 0; pass < 2; pass++) {
      for (let i = 0; i < 20; i++) await Promise.resolve();
      drawPage();
    }
  }

  function layer(): HTMLElement {
    return root.querySelector<HTMLElement>("main > .suggestion-layer")!;
  }

  beforeEach(() => {
    onElement = vi.fn();
  });

  it("draws the shown pills in the layer over the transcript, and the line alone in the footer", async () => {
    stored.nextSteps = nextSteps("a-1", "fixed");
    await drawPageAndSettle();

    const group = layer().querySelector<HTMLElement>('[data-placement="next_step"]')!;
    expect(group.getAttribute("role")).toBe("group");
    expect(group.getAttribute("aria-label")).toBe("Suggestions");
    expect([...group.querySelectorAll(".suggestion-pill")].map((pill) => pill.textContent)).toEqual([
      "Go ahead",
      "Change the plan",
    ]);
    expect(root.querySelectorAll("footer .suggestion-pill")).toHaveLength(0);
    expect(root.querySelectorAll("main .suggestion-line")).toHaveLength(0);
    expect(root.querySelector("footer .chat-suggestions .suggestion-line")?.textContent).toBe(
      "disable suggestionsHide suggestions",
    );
  });

  it("ends the scroll element at its bottom edge, at most 60% of its height, scrolling a taller list inside", async () => {
    stored.nextSteps = nextSteps("a-1", "fixed");
    await drawPageAndSettle();

    expect([...layer().classList]).toEqual(expect.arrayContaining(["sticky", "bottom-0", "mt-auto", "max-h-[60%]"]));
    expect(layer().nextElementSibling).toBeNull();
    const scroller = layer().querySelector<HTMLElement>(".suggestion-layer-scroll")!;
    expect([...scroller.classList]).toEqual(expect.arrayContaining(["min-h-0", "overflow-y-auto"]));
    // The pills alone rise in.
    expect(scroller.classList.contains("suggestion-rise")).toBe(true);
    expect(scroller.firstElementChild?.classList.contains("suggestion-strip-pills")).toBe(true);
  });

  it("holds nothing while the pills wait behind their line, and is still there", async () => {
    stored.nextSteps = nextSteps("a-1");
    await drawPageAndSettle();

    expect(layer().childElementCount).toBe(0);
    expect(root.querySelector("footer .suggestion-line")?.textContent).toBe("See 2 suggestions");

    click("footer .suggestion-line-toggle");
    drawPage();
    expect(layer().querySelectorAll(".suggestion-pill")).toHaveLength(2);

    click("footer .suggestion-line-toggle");
    drawPage();
    expect(layer().childElementCount).toBe(0);
  });

  it("draws a new-chat strip's pills there too, under its own name", async () => {
    newAndEmpty();
    await drawPageAndSettle();

    const group = layer().querySelector<HTMLElement>('[data-placement="new_chat"]')!;
    expect(group.getAttribute("aria-label")).toBe("Things to try");
    expect(group.querySelectorAll(".suggestion-pill")).toHaveLength(FIRST_THREE_LABELS.length);
    expect(root.querySelector("footer .suggestion-line")?.textContent).toBe("disable new chat suggestions");
    expect(root.querySelectorAll("footer .suggestion-pill")).toHaveLength(0);
  });

  it("draws another reply's pills as new ones, so they rise in rather than change in place", async () => {
    stored.nextSteps = nextSteps("a-1", "fixed");
    await drawPageAndSettle();
    const first = layer().firstElementChild;

    mocks.events = [userMessage("u-1"), reply("a-1"), userMessage("u-2"), reply("a-2")];
    stored.nextSteps = nextSteps("a-2", "fixed");
    resetSuggestionsForTests();
    await drawPageAndSettle();

    expect(layer().firstElementChild).not.toBeNull();
    expect(layer().firstElementChild).not.toBe(first);
  });

  it("tells its element once it is drawn, and null as it goes", async () => {
    await drawPageAndSettle();
    expect(onElement.mock.calls).toEqual([[layer()]]);

    m.render(root, null);
    expect(onElement.mock.calls).toHaveLength(2);
    expect(onElement.mock.calls[1]).toEqual([null]);
  });
});
