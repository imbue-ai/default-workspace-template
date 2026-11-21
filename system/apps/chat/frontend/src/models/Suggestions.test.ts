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
  collapseNextStepSuggestions,
  getNextStepSuggestions,
  type NextStepFacts,
  readNextStepSuggestions,
  reconcileNextStepSuggestions,
  rereadNextStepSuggestions,
  resetSuggestionsForTests,
  RETRY_DELAY_MS,
  type SuggestionsAnswer,
  suggestionSetOf,
  toggleNextStepSuggestions,
} from "./Suggestions";

const CHAT = "agent-1";
const NONE: SuggestionsAnswer = { status: "none", after_event_id: null, source: null, suggestions: [] };

/** What the route answers while a set for ``afterEventId`` is being worked out. */
function pending(afterEventId: string): SuggestionsAnswer {
  return { status: "pending", after_event_id: afterEventId, source: null, suggestions: [] };
}

function ready(afterEventId: string, labels: string[] = ["Yes", "No"]): SuggestionsAnswer {
  return {
    status: "ready",
    after_event_id: afterEventId,
    source: "fixed",
    suggestions: labels.map((label) => ({ suggestion_id: label.toLowerCase(), label, is_recommended: false })),
  };
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
  it("reads the stored set when the page first looks, and draws it open under the reply it follows", async () => {
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

  it("collapses to its toggle when a message is sent, and opens again from it", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();

    collapseNextStepSuggestions(CHAT);
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(false);
    expect(shownLabels()).toEqual(["Yes", "No"]);

    toggleNextStepSuggestions(CHAT);
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
    expect(shownLabels()).toEqual(["Yes", "No"]);
  });

  it("collapses when a turn starts, whoever started it, and stays reopened through that turn", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();

    await look({ isTurnDone: false, lastReplyEventId: null });
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(false);

    toggleNextStepSuggestions(CHAT);
    await look({ isTurnDone: false, lastReplyEventId: "reply-2" });
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(true);
    expect(shownLabels()).toEqual(["Yes", "No"]);
  });

  it("is dropped when the next turn ends, whether or not the new reply has suggestions, and reads again", async () => {
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

  it("comes back open when the send that collapsed it never left the page", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    // The send is on its way, then fails: nothing reached the chat, and the stored set is still the reply's.
    await look({ isTurnDone: false });
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(false);

    await look();

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

  it("keeps a collapsed strip collapsed when the same set is read again", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    collapseNextStepSuggestions(CHAT);

    rereadNextStepSuggestions(CHAT);
    await look();

    expect(mockRequest).toHaveBeenCalledTimes(2);
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(false);
  });

  it("replaces the drawn set with a different one read for the same reply, open", async () => {
    mockRequest.mockResolvedValue(ready("reply-1"));
    await look();
    collapseNextStepSuggestions(CHAT);

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
    expect(shownLabels()).toEqual(["Yes", "No"]);
    expect(getNextStepSuggestions(CHAT)?.isOpen).toBe(false);

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
