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
  getAnsweredReplySuggestions,
  getNextStepSuggestions,
  type NextStepFacts,
  readNextStepSuggestions,
  reconcileNextStepSuggestions,
  rereadNextStepSuggestions,
  resetSuggestionsForTests,
  RETRY_DELAY_MS,
  type ServedSuggestionSet,
  type SuggestionsAnswer,
  suggestionSetOf,
  toggleAnsweredReplySuggestions,
  toggleNextStepSuggestions,
} from "./Suggestions";
import {
  NO_SUGGESTIONS_ANSWER as NONE,
  pendingSuggestionsAnswerFixture as pending,
  storedSuggestionsAnswerFixture,
  suggestionSetFixture,
} from "./suggestionsAnswerFixture";

const CHAT = "agent-1";

/** A set as the chat app remembers it for the reply ``afterEventId``. */
function remembered(
  afterEventId: string,
  labels: string[] = ["Yes", "No"],
  source: string = "fixed",
): ServedSuggestionSet {
  return suggestionSetFixture(afterEventId, labels, source);
}

/** What the route answers with a set stored for ``afterEventId``, which the chat app remembers after ``earlier``. */
function ready(
  afterEventId: string,
  labels: string[] = ["Yes", "No"],
  source: string = "fixed",
  earlier: ServedSuggestionSet[] = [],
): SuggestionsAnswer {
  return storedSuggestionsAnswerFixture([...earlier, remembered(afterEventId, labels, source)]);
}

/** A reply's turn is done, next-step suggestions are on, and ``lastReplyEventId`` is the chat's last reply. */
function facts(overrides: Partial<NextStepFacts> = {}): NextStepFacts {
  return { isEnabled: true, isRoom: false, isTurnDone: true, lastReplyEventId: "reply-1", ...overrides };
}

/** Let the reads the page asked for answer. */
async function settle(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

/** Look at the chat as a draw does, and let any read it asked for answer. */
async function look(overrides: Partial<NextStepFacts> = {}): Promise<void> {
  reconcileNextStepSuggestions(CHAT, facts(overrides));
  await settle();
  reconcileNextStepSuggestions(CHAT, facts(overrides));
}

function shownLabels(): string[] | null {
  return getNextStepSuggestions(CHAT)?.set.suggestions.map((suggestion) => suggestion.label) ?? null;
}

/** The set drawn in the transcript under the reply ``eventId``, with the chat as ``overrides`` have it. */
function answeredReply(eventId: string, overrides: Partial<NextStepFacts> = {}) {
  return getAnsweredReplySuggestions(CHAT, eventId, () => facts(overrides));
}

function answeredLabels(eventId: string, overrides: Partial<NextStepFacts> = {}): string[] | null {
  return answeredReply(eventId, overrides)?.set.suggestions.map((suggestion) => suggestion.label) ?? null;
}

beforeEach(() => {
  resetSuggestionsForTests();
  mockRequest.mockReset();
  mockRequest.mockResolvedValue(NONE);
  mockRedraw.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("suggestionSetOf", () => {
  it("reads a ready answer as its set", () => {
    expect(suggestionSetOf(ready("reply-1"))).toEqual({
      after_event_id: "reply-1",
      source: "fixed",
      suggestions: [
        { suggestion_id: "yes", label: "Yes", is_recommended: false },
        { suggestion_id: "no", label: "No", is_recommended: false },
      ],
    });
  });

  it.each(["none", "pending", "a-status-from-the-future"])("reads the status %s as no set", (status) => {
    expect(suggestionSetOf({ ...ready("reply-1"), status })).toBeNull();
  });

  it("reads a pending answer as no set yet", () => {
    expect(suggestionSetOf(pending("reply-1"))).toBeNull();
  });

  it("reads a ready answer with nothing to draw as no set", () => {
    expect(suggestionSetOf({ ...ready("reply-1"), suggestions: [] })).toBeNull();
    expect(suggestionSetOf({ ...ready("reply-1"), after_event_id: null })).toBeNull();
  });

  it("keeps the recommended mark on the first suggestion only", () => {
    const answer = ready("reply-1", ["One", "Two"]);
    const recommended = answer.suggestions.map((suggestion) => ({ ...suggestion, is_recommended: true }));
    expect(suggestionSetOf({ ...answer, suggestions: recommended })?.suggestions.map((s) => s.is_recommended)).toEqual(
      [true, false],
    );
  });

  it("drops a suggestion that asks for an action this page does not know", () => {
    const answer = ready("reply-1", ["Reply", "Act"]);
    const suggestions = [answer.suggestions[0], { ...answer.suggestions[1], action: "phone_access" }];
    expect(suggestionSetOf({ ...answer, suggestions })?.suggestions.map((s) => s.label)).toEqual(["Reply"]);
  });

  it("reads a source it does not know as none, keeping the set", () => {
    expect(suggestionSetOf({ ...ready("reply-1"), source: "imported" })?.source).toBeNull();
  });
});

describe("the set a chat's page draws", () => {
  it("reads the stored set when the page first looks, and draws a fixed one with its pills shown", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));

    await look();

    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(mockRequest).toHaveBeenCalledWith({
      method: "GET",
      url: "/api/chats/:chatId/suggestions",
      params: { chatId: CHAT },
    });
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
    expect(shownLabels()).toEqual(["Yes", "No"]);
  });

  it("never draws a set stored for a reply that is not the chat's last", async () => {
    mockRequest.mockResolvedValue(ready("reply-0"));

    await look({ lastReplyEventId: "reply-1" });
    expect(getNextStepSuggestions(CHAT)).toBeNull();

    // Nor under no reply at all: a transcript still loading, or one that ends on somebody's message.
    await look({ lastReplyEventId: null });
    expect(getNextStepSuggestions(CHAT)).toBeNull();
  });

  it("draws the set once the transcript shows the reply it follows", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look({ lastReplyEventId: null });
    expect(getNextStepSuggestions(CHAT)).toBeNull();

    await look({ lastReplyEventId: "reply-1" });

    expect(shownLabels()).toEqual(["Yes", "No"]);
    expect(mockRequest).toHaveBeenCalledTimes(1);
  });

  it("draws nothing while the turn runs, in a room, or with the setting off or not yet read", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));

    await look({ isTurnDone: false });
    expect(getNextStepSuggestions(CHAT)).toBeNull();

    resetSuggestionsForTests();
    await look({ isEnabled: false });
    expect(getNextStepSuggestions(CHAT)).toBeNull();

    resetSuggestionsForTests();
    mockRequest.mockClear();
    await look({ isRoom: true });
    expect(getNextStepSuggestions(CHAT)).toBeNull();
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("draws a generated set behind its line, and one of a source it does not know", async () => {
    mockRequest.mockResolvedValue(ready("reply-1", ["Yes", "No"], "generated"));
    await look();
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(false);
    expect(shownLabels()).toEqual(["Yes", "No"]);

    resetSuggestionsForTests();
    mockRequest.mockResolvedValue(ready("reply-1", ["Yes", "No"], "imported"));
    await look();
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(false);
  });

  it("shows its pills from its line and hides them again", async () => {
    mockRequest.mockResolvedValue(ready("reply-1", ["Yes", "No"], "generated"));
    await look();

    toggleNextStepSuggestions(CHAT);
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
    toggleNextStepSuggestions(CHAT);
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(false);
    expect(shownLabels()).toEqual(["Yes", "No"]);
  });

  it("leaves the composer the moment a turn starts, whoever started it", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();

    // A message is on its way: the reply is still the chat's last, and its turn is no longer done.
    reconcileNextStepSuggestions(CHAT, facts({ isTurnDone: false }));
    expect(getNextStepSuggestions(CHAT)).toBeNull();

    await look({ isTurnDone: false, lastReplyEventId: null });
    await look({ isTurnDone: false, lastReplyEventId: "reply-2" });
    expect(getNextStepSuggestions(CHAT)).toBeNull();
    expect(mockRequest).toHaveBeenCalledTimes(1);
  });

  it("stays away when the next turn ends, whether or not the new reply has suggestions, and reads again", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    await look({ isTurnDone: false, lastReplyEventId: "reply-2" });
    expect(mockRequest).toHaveBeenCalledTimes(1);

    mockRequest.mockResolvedValue(NONE);
    await look({ lastReplyEventId: "reply-2" });

    expect(getNextStepSuggestions(CHAT)).toBeNull();
    expect(mockRequest).toHaveBeenCalledTimes(2);
  });

  it("gives way to the next reply's set, open, when that turn ends with one", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    await look({ isTurnDone: false, lastReplyEventId: "reply-2" });

    mockRequest.mockResolvedValue(ready("reply-2", ["More ideas", "Something else"]));
    await look({ lastReplyEventId: "reply-2" });

    expect(shownLabels()).toEqual(["More ideas", "Something else"]);
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
  });

  it("does not bring the old set back while the read a turn's end asked for is out", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    reconcileNextStepSuggestions(CHAT, facts({ isTurnDone: false }));

    // The turn ended with no new reply (it was stopped), and the backend has not answered yet.
    mockRequest.mockReturnValue(new Promise(() => undefined));
    reconcileNextStepSuggestions(CHAT, facts());
    reconcileNextStepSuggestions(CHAT, facts());

    expect(getNextStepSuggestions(CHAT)).toBeNull();
  });

  it("comes back above the composer when the send that took it away never left the page", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    // The send is on its way, then fails: nothing reached the chat, and the stored set is still the reply's.
    await look({ isTurnDone: false });
    expect(getNextStepSuggestions(CHAT)).toBeNull();
    expect(answeredLabels("reply-1", { isTurnDone: false })).toEqual(["Yes", "No"]);

    await look();

    expect(answeredReply("reply-1")).toBeNull();

    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
    expect(shownLabels()).toEqual(["Yes", "No"]);
  });

  it("is dropped the moment the setting goes off, and drawn again when it comes back", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();

    reconcileNextStepSuggestions(CHAT, facts({ isEnabled: false }));
    expect(getNextStepSuggestions(CHAT)).toBeNull();

    reconcileNextStepSuggestions(CHAT, facts());
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
  });

  it("keeps a set's pills hidden when the same set is read again", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    toggleNextStepSuggestions(CHAT);

    rereadNextStepSuggestions(CHAT);
    await look();

    expect(mockRequest).toHaveBeenCalledTimes(2);
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(false);
  });

  it("replaces the drawn set with a different one read for the same reply, as a set newly drawn", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    toggleNextStepSuggestions(CHAT);

    mockRequest.mockResolvedValue(ready("reply-1", ["Show me some ideas"]));
    rereadNextStepSuggestions(CHAT);
    await look();

    expect(shownLabels()).toEqual(["Show me some ideas"]);
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
  });
});

describe("a set worked out after the turn ended", () => {
  it("is drawn, open, when the push that says it is stored brings it, with no wait in between", async () => {
    vi.useFakeTimers();
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    await look({ isTurnDone: false, lastReplyEventId: "reply-2" });

    // The turn ends: the read it asks for finds the new reply's set still being worked out.
    mockRequest.mockResolvedValue(pending("reply-2"));
    await look({ lastReplyEventId: "reply-2" });
    expect(getNextStepSuggestions(CHAT)).toBeNull();
    expect(mockRequest).toHaveBeenCalledTimes(2);

    // Looking again asks nothing: the push is what says there is something to read.
    await look({ lastReplyEventId: "reply-2" });
    expect(mockRequest).toHaveBeenCalledTimes(2);

    mockRequest.mockResolvedValue(ready("reply-2", ["Yes, add reminders", "Show me the app"]));
    rereadNextStepSuggestions(CHAT);
    await look({ lastReplyEventId: "reply-2" });

    expect(shownLabels()).toEqual(["Yes, add reminders", "Show me the app"]);
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
    expect(mockRequest).toHaveBeenCalledTimes(3);
  });

  it("is drawn when its push arrives before the page has seen the turn end", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    await look({ isTurnDone: false, lastReplyEventId: "reply-2" });

    mockRequest.mockResolvedValue(ready("reply-2", ["Thanks, that is all"]));
    rereadNextStepSuggestions(CHAT);
    await look({ isTurnDone: false, lastReplyEventId: "reply-2" });
    expect(getNextStepSuggestions(CHAT)).toBeNull();

    await look({ lastReplyEventId: "reply-2" });

    expect(shownLabels()).toEqual(["Thanks, that is all"]);
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
  });

  it("leaves nothing drawn when the push says the stored set was removed", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    expect(shownLabels()).toEqual(["Yes", "No"]);

    mockRequest.mockResolvedValue(NONE);
    rereadNextStepSuggestions(CHAT);
    await look({ isTurnDone: false });
    await look({ lastReplyEventId: "reply-2" });

    expect(getNextStepSuggestions(CHAT)).toBeNull();
  });
});

describe("the set a reply keeps in the transcript once it has been answered", () => {
  it("is drawn under its reply the moment a turn starts, behind its line, with nothing read again", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
    expect(answeredReply("reply-1")).toBeNull();

    // A message is on its way, and then is the chat's last word.
    expect(answeredReply("reply-1", { isTurnDone: false })).toEqual({
      set: getNextStepSuggestions(CHAT)?.set,
      isOpen: false,
    });
    expect(answeredLabels("reply-1", { isTurnDone: false, lastReplyEventId: null })).toEqual(["Yes", "No"]);
    expect(answeredLabels("reply-1", { lastReplyEventId: "reply-2" })).toEqual(["Yes", "No"]);
    expect(mockRequest).toHaveBeenCalledTimes(1);
  });

  it("shows its pills from its line and hides them again, each reply's line by itself", async () => {
    mockRequest.mockResolvedValue(ready("reply-2", ["More ideas"], "generated", [remembered("reply-1")]));
    await look({ lastReplyEventId: "reply-3" });

    toggleAnsweredReplySuggestions(CHAT, "reply-1");
    expect(answeredReply("reply-1", { lastReplyEventId: "reply-3" })?.isOpen).toBe(true);
    expect(answeredReply("reply-2", { lastReplyEventId: "reply-3" })?.isOpen).toBe(false);
    expect(mockRedraw).toHaveBeenCalled();

    toggleAnsweredReplySuggestions(CHAT, "reply-1");
    expect(answeredReply("reply-1", { lastReplyEventId: "reply-3" })?.isOpen).toBe(false);

    // A reply the page knows no set for has no line to open.
    toggleAnsweredReplySuggestions(CHAT, "reply-9");
    expect(answeredReply("reply-9", { lastReplyEventId: "reply-3" })).toBeNull();
  });

  it("is kept for every answered reply the chat app remembers a set for, and none for the reply waited on", async () => {
    mockRequest.mockResolvedValue(
      ready("reply-3", ["Thanks"], "generated", [remembered("reply-1"), remembered("reply-2", ["More ideas"])]),
    );
    await look({ lastReplyEventId: "reply-3" });

    expect(answeredLabels("reply-1", { lastReplyEventId: "reply-3" })).toEqual(["Yes", "No"]);
    expect(answeredLabels("reply-2", { lastReplyEventId: "reply-3" })).toEqual(["More ideas"]);
    expect(answeredReply("reply-3", { lastReplyEventId: "reply-3" })).toBeNull();
    expect(shownLabels()).toEqual(["Thanks"]);
  });

  it("stays known when a later answer leaves it out, as the chat app's does after a restart", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    await look({ isTurnDone: false, lastReplyEventId: "reply-2" });

    mockRequest.mockResolvedValue(ready("reply-2", ["More ideas"]));
    await look({ lastReplyEventId: "reply-2" });

    expect(answeredLabels("reply-1", { lastReplyEventId: "reply-2" })).toEqual(["Yes", "No"]);
    expect(shownLabels()).toEqual(["More ideas"]);
  });

  it("starts behind its line though its pills were shown above the composer, each time it is answered", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
    await look({ isTurnDone: false });
    toggleAnsweredReplySuggestions(CHAT, "reply-1");
    expect(answeredReply("reply-1", { isTurnDone: false })?.isOpen).toBe(true);

    // The turn ends with nothing new said, so the reply is waited on again, and then a message is sent.
    await look();
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);

    expect(answeredReply("reply-1", { isTurnDone: false })?.isOpen).toBe(false);
  });

  it("is not drawn in a room, or with the setting off or not yet read", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();

    expect(answeredReply("reply-1", { lastReplyEventId: "reply-2", isRoom: true })).toBeNull();
    expect(answeredReply("reply-1", { lastReplyEventId: "reply-2", isEnabled: false })).toBeNull();
    expect(answeredLabels("reply-1", { lastReplyEventId: "reply-2" })).toEqual(["Yes", "No"]);
  });

  it("is not drawn for a set with nothing left to draw, which also replaces the one known for its reply", async () => {
    mockRequest.mockResolvedValue(ready("reply-2", ["More ideas"], "generated", [remembered("reply-1")]));
    await look({ lastReplyEventId: "reply-3" });
    expect(answeredLabels("reply-1", { lastReplyEventId: "reply-3" })).toEqual(["Yes", "No"]);

    const actionsOnly = remembered("reply-1", ["Open the share panel"]);
    mockRequest.mockResolvedValue({
      ...NONE,
      sets: [{ ...actionsOnly, suggestions: [{ ...actionsOnly.suggestions[0], action: "phone_access" }] }],
    });
    rereadNextStepSuggestions(CHAT);
    await settle();

    expect(answeredReply("reply-1", { lastReplyEventId: "reply-3" })).toBeNull();
    expect(answeredLabels("reply-2", { lastReplyEventId: "reply-3" })).toEqual(["More ideas"]);
  });

  it("reads nothing of the chat for a reply the page knows no set for, nor for a chat it has never looked at", async () => {
    const readFacts = vi.fn(() => facts());
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();

    expect(getAnsweredReplySuggestions(CHAT, "reply-0", readFacts)).toBeNull();
    expect(getAnsweredReplySuggestions("agent-elsewhere", "reply-1", readFacts)).toBeNull();
    expect(readFacts).not.toHaveBeenCalled();

    expect(getAnsweredReplySuggestions(CHAT, "reply-1", readFacts)).toBeNull();
    expect(readFacts).toHaveBeenCalledTimes(1);
  });
});

describe("reading the stored set", () => {
  it("reads again when the settings change, for a chat the page draws suggestions for", async () => {
    await look();
    expect(mockRequest).toHaveBeenCalledTimes(1);

    mockRequest.mockResolvedValue(ready("reply-1"));
    for (const listener of settingsListeners) listener();
    await look();

    expect(mockRequest).toHaveBeenCalledTimes(2);
    expect(shownLabels()).toEqual(["Yes", "No"]);
  });

  it("reads nothing again for a chat the page has never drawn suggestions for", () => {
    rereadNextStepSuggestions("agent-elsewhere");

    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("drops an answer that a later read has overtaken", async () => {
    let answerFirst: (answer: SuggestionsAnswer) => void = () => undefined;
    mockRequest.mockReturnValueOnce(new Promise<SuggestionsAnswer>((resolve) => (answerFirst = resolve)));
    mockRequest.mockResolvedValueOnce(ready("reply-1", ["Newer"]));

    const first = readNextStepSuggestions(CHAT);
    await readNextStepSuggestions(CHAT);
    answerFirst(ready("reply-1", ["Older"]));
    await first;
    await look();

    expect(shownLabels()).toEqual(["Newer"]);
  });

  it("warns about a failed read and asks again only after the retry delay, however often the page looks", async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mockRequest.mockRejectedValueOnce(new Error("404"));

    await look();
    await look();
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(getNextStepSuggestions(CHAT)).toBeNull();

    vi.advanceTimersByTime(RETRY_DELAY_MS);
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();

    expect(mockRequest).toHaveBeenCalledTimes(2);
    expect(shownLabels()).toEqual(["Yes", "No"]);
  });
});
