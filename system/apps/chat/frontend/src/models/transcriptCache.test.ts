import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TranscriptEvent } from "./Response";
import {
  DEFAULT_SCREEN,
  MAX_ENTRY_CHARS,
  ROW_CHROME_CHARS,
  TOTAL_BUDGET_CHARS,
  currentScreen,
  dropCachedTranscript,
  pruneCachedTranscripts,
  readCachedTranscript,
  screenChars,
  setScreenMeasurer,
  tailForScreen,
  textTail,
  touchCachedTranscript,
  writeCachedTranscript,
  type ScreenMeasure,
} from "./transcriptCache";
import { memoryStorage } from "./storageFixture";

function makeEvent(id: string, content: string = id): TranscriptEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "user_message",
    event_id: id,
    source: "test",
    message_uuid: id,
    role: "user",
    content,
  };
}

function makeEvents(count: number, prefix = "e", content?: string): TranscriptEvent[] {
  return Array.from({ length: count }, (_, index) => makeEvent(`${prefix}${index}`, content));
}

function contentOf(event: TranscriptEvent): string {
  return event.type === "user_message" ? event.content : "";
}

// A screen holding 1,000 characters of rendered text with its margin.
const SMALL_SCREEN: ScreenMeasure = { heightPx: 200, charsPerPx: 4 };

/** Characters one chat's entry takes in storage. */
function storedChars(chatId: string): number {
  return sessionStorage.getItem(`transcript-cache:${chatId}`)?.length ?? 0;
}

describe("the snapshot of a chat's screen", () => {
  it("keeps the last events whose text fills the screen and its margin, starting where the first of them sits", () => {
    // Each event renders 100 characters of text plus its row's chrome.
    const events = makeEvents(50, "e", "x".repeat(100));

    const tail = tailForScreen(events, 200, SMALL_SCREEN);

    const perEvent = 100 + ROW_CHROME_CHARS;
    const expectedCount = Math.ceil(screenChars(SMALL_SCREEN) / perEvent);
    expect(tail.events).toHaveLength(expectedCount);
    expect(tail.events[tail.events.length - 1]?.event_id).toBe("e49");
    expect(tail.offset).toBe(200 + 50 - expectedCount);
  });

  it("keeps more of the chat for a taller screen", () => {
    const events = makeEvents(50, "e", "x".repeat(100));

    const short = tailForScreen(events, 0, SMALL_SCREEN);
    const tall = tailForScreen(events, 0, { heightPx: 600, charsPerPx: 4 });

    expect(tall.events.length).toBeGreaterThan(short.events.length * 2);
  });

  it("keeps only the bottom of one huge message, so it costs no more than a screen", () => {
    const huge = `${"head ".repeat(100_000)}\nthe last line`;

    const tail = tailForScreen([makeEvent("a"), makeEvent("huge", huge)], 0, SMALL_SCREEN);

    expect(tail.events.map((event) => event.event_id)).toEqual(["huge"]);
    expect(contentOf(tail.events[0]).endsWith("the last line")).toBe(true);
    expect(contentOf(tail.events[0]).length).toBeLessThanOrEqual(screenChars(SMALL_SCREEN));
  });

  it("does not count events that render nothing toward the screen", () => {
    const hidden: TranscriptEvent = {
      ...makeEvent("hidden", "y".repeat(5_000)),
      display: "hidden",
    } as TranscriptEvent;
    const events = [...makeEvents(3, "e", "x".repeat(100)), hidden];

    const tail = tailForScreen(events, 0, SMALL_SCREEN);

    expect(tail.events.map((event) => event.event_id)).toEqual(["e0", "e1", "e2", "hidden"]);
  });

  it("cuts a message's text at a line, reopening a code fence the cut fell inside", () => {
    const text = [
      "intro",
      "```ts",
      ...Array.from({ length: 40 }, (_, index) => `const line${index} = ${index};`),
      "```",
      "done",
    ].join("\n");

    const tail = textTail(text, 80);

    expect(tail.startsWith("```\n")).toBe(true);
    expect(tail.endsWith("```\ndone")).toBe(true);
    expect(tail.split("\n")[1]).toMatch(/^const line\d+ = \d+;$/);
    expect(textTail("short", 80)).toBe("short");
  });
});

describe("the transcript cache", () => {
  beforeEach(() => {
    vi.stubGlobal("sessionStorage", memoryStorage());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setScreenMeasurer(() => null);
  });

  it("reads back what was written, placed where it sits in the transcript", () => {
    writeCachedTranscript("chat-a", makeEvents(3), 10, 13);

    const cached = readCachedTranscript("chat-a");

    expect(cached?.events.map((event) => event.event_id)).toEqual(["e0", "e1", "e2"]);
    expect(cached?.offset).toBe(10);
    expect(cached?.total).toBe(13);
  });

  it("never lets one chat's entry grow past its cap, keeping the latest events", () => {
    // A screen so dense it would take far more than the cap.
    const events = makeEvents(400, "e", "x".repeat(400));
    writeCachedTranscript("chat-a", events, 0, 400, { heightPx: 10_000, charsPerPx: 10 });

    const cached = readCachedTranscript("chat-a");

    expect(storedChars("chat-a")).toBeLessThanOrEqual(MAX_ENTRY_CHARS);
    expect(cached?.events[cached.events.length - 1]?.event_id).toBe("e399");
    expect(cached?.offset).toBe(400 - (cached?.events.length ?? 0));
  });

  it("holds all chats within one budget, evicting the chat viewed longest ago first", () => {
    const content = "z".repeat(800);
    const screen: ScreenMeasure = { heightPx: 2_000, charsPerPx: 4 };
    let written = 0;
    while (written * 20_000 < TOTAL_BUDGET_CHARS * 2) {
      writeCachedTranscript(`chat-${written}`, makeEvents(30, "e", content), 0, 30, screen);
      written += 1;
    }

    const kept = Array.from({ length: written }, (_, index) => `chat-${index}`).filter(
      (chatId) => readCachedTranscript(chatId) !== null,
    );
    const total = kept.reduce((sum, chatId) => sum + storedChars(chatId), 0);
    expect(total).toBeLessThanOrEqual(TOTAL_BUDGET_CHARS);
    expect(kept.length).toBeLessThan(written);
    // The survivors are the latest written, in one unbroken run up to the newest.
    expect(kept).toEqual(Array.from({ length: kept.length }, (_, index) => `chat-${written - kept.length + index}`));
  });

  /** Write chats of one 20,000-character message each until the budget holds no more; their ids, oldest first. */
  function fillBudget(prefix: string): string[] {
    const write = (chatId: string): void =>
      writeCachedTranscript(chatId, makeEvents(1, "e", "q".repeat(20_000)), 0, 1, { heightPx: 20_000, charsPerPx: 4 });
    write(`${prefix}0`);
    const entryChars = storedChars(`${prefix}0`);
    const ids = [`${prefix}0`];
    while ((ids.length + 1) * entryChars <= TOTAL_BUDGET_CHARS) {
      ids.push(`${prefix}${ids.length}`);
      write(ids[ids.length - 1]);
    }
    return ids;
  }

  it("keeps a chat that was viewed again, evicting the next oldest instead", () => {
    const ids = fillBudget("chat-");
    expect(ids.every((chatId) => readCachedTranscript(chatId) !== null)).toBe(true);

    touchCachedTranscript(ids[0]);
    writeCachedTranscript("chat-new", makeEvents(1, "e", "q".repeat(20_000)), 0, 1, {
      heightPx: 20_000,
      charsPerPx: 4,
    });

    expect(readCachedTranscript(ids[0])).not.toBeNull();
    expect(readCachedTranscript(ids[1])).toBeNull();
    expect(readCachedTranscript("chat-new")).not.toBeNull();
  });

  it("does not count a rewrite as viewing, so a chat streaming behind another keeps its place", () => {
    const ids = fillBudget("chat-");

    writeCachedTranscript(ids[0], makeEvents(2, "e", "q".repeat(10_000)), 0, 2, { heightPx: 20_000, charsPerPx: 4 });
    writeCachedTranscript("chat-new", makeEvents(1, "e", "q".repeat(20_000)), 0, 1, {
      heightPx: 20_000,
      charsPerPx: 4,
    });

    expect(readCachedTranscript(ids[0])).toBeNull();
    expect(readCachedTranscript(ids[1])).not.toBeNull();
    expect(readCachedTranscript("chat-new")).not.toBeNull();
  });

  it("drops a chat viewed longest ago rather than grow past the budget when it changes", () => {
    writeCachedTranscript("chat-old", makeEvents(1, "e", "s".repeat(1_000)), 0, 1);
    const ids = fillBudget("chat-");
    expect(readCachedTranscript("chat-old")).not.toBeNull();

    writeCachedTranscript("chat-old", makeEvents(1, "e", "s".repeat(20_000)), 0, 1, {
      heightPx: 20_000,
      charsPerPx: 4,
    });

    expect(readCachedTranscript("chat-old")).toBeNull();
    expect(ids.every((chatId) => readCachedTranscript(chatId) !== null)).toBe(true);
    expect(ids.reduce((sum, chatId) => sum + storedChars(chatId), 0)).toBeLessThanOrEqual(TOTAL_BUDGET_CHARS);
  });

  it("makes room over quota by dropping the least recently viewed other chats", () => {
    const content = "y".repeat(1000);
    vi.stubGlobal("sessionStorage", memoryStorage(2800));
    writeCachedTranscript("chat-old", [makeEvent("a", content)], 0, 1);
    writeCachedTranscript("chat-mid", [makeEvent("b", content)], 0, 1);
    writeCachedTranscript("chat-new", [makeEvent("c", content)], 0, 1);

    expect(readCachedTranscript("chat-old")).toBeNull();
    expect(readCachedTranscript("chat-mid")).not.toBeNull();
    expect(readCachedTranscript("chat-new")).not.toBeNull();
  });

  it("sizes a write by the screen the page measured, and by the last one while it is hidden", () => {
    const measured: ScreenMeasure = { heightPx: 300, charsPerPx: 2 };
    let isHidden = false;
    setScreenMeasurer(() => (isHidden ? null : measured));

    expect(currentScreen()).toEqual(measured);
    isHidden = true;
    expect(currentScreen()).toEqual(measured);

    setScreenMeasurer(() => null);
  });

  it("uses a default screen before the page has measured one", () => {
    vi.resetModules();
    return import("./transcriptCache").then((fresh) => {
      expect(fresh.currentScreen()).toEqual(DEFAULT_SCREEN);
    });
  });

  it("reads a corrupt or foreign entry as nothing cached", () => {
    sessionStorage.setItem("transcript-cache:chat-a", "{not json");
    sessionStorage.setItem("transcript-cache:chat-b", JSON.stringify({ version: 999, events: [], offset: 0 }));

    expect(readCachedTranscript("chat-a")).toBeNull();
    expect(readCachedTranscript("chat-b")).toBeNull();
  });

  it("forgets a dropped chat", () => {
    writeCachedTranscript("chat-a", makeEvents(2), 0, 2);

    dropCachedTranscript("chat-a");

    expect(readCachedTranscript("chat-a")).toBeNull();
    expect(sessionStorage.getItem("transcript-cache-index")).toBe("[]");
  });

  it("forgets every chat no longer kept, and only those", () => {
    writeCachedTranscript("chat-a", makeEvents(1), 0, 1);
    writeCachedTranscript("chat-b", makeEvents(1), 0, 1);

    pruneCachedTranscripts((chatId) => chatId === "chat-b");

    expect(readCachedTranscript("chat-a")).toBeNull();
    expect(readCachedTranscript("chat-b")).not.toBeNull();
  });

  it("does nothing where there is no session storage", () => {
    vi.unstubAllGlobals();
    vi.stubGlobal("sessionStorage", undefined);

    writeCachedTranscript("chat-a", makeEvents(2), 0, 2);
    touchCachedTranscript("chat-a");
    pruneCachedTranscripts(() => false);

    expect(readCachedTranscript("chat-a")).toBeNull();
  });
});
