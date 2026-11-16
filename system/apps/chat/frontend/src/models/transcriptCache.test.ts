import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TranscriptEvent } from "./Response";
import {
  CACHED_EVENT_LIMIT,
  MAX_CACHED_CHATS,
  MAX_ENTRY_CHARS,
  dropCachedTranscript,
  readCachedTranscript,
  writeCachedTranscript,
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

function makeEvents(count: number, prefix = "e"): TranscriptEvent[] {
  return Array.from({ length: count }, (_, index) => makeEvent(`${prefix}${index}`));
}

describe("transcript cache", () => {
  beforeEach(() => {
    vi.stubGlobal("sessionStorage", memoryStorage());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reads back what was written, placed where it sits in the transcript", () => {
    writeCachedTranscript("chat-a", makeEvents(3), 10, 13);

    const cached = readCachedTranscript("chat-a");

    expect(cached?.events.map((event) => event.event_id)).toEqual(["e0", "e1", "e2"]);
    expect(cached?.offset).toBe(10);
    expect(cached?.total).toBe(13);
  });

  it("keeps only the tail of a long window, with the offset moved to its first kept event", () => {
    const events = makeEvents(CACHED_EVENT_LIMIT + 5);
    writeCachedTranscript("chat-a", events, 100, 100 + events.length);

    const cached = readCachedTranscript("chat-a");

    expect(cached?.events).toHaveLength(CACHED_EVENT_LIMIT);
    expect(cached?.events[0].event_id).toBe("e5");
    expect(cached?.offset).toBe(105);
    expect(cached?.total).toBe(100 + events.length);
  });

  it("keeps fewer events when the tail would be too large, and none when one event is", () => {
    const big = "x".repeat(MAX_ENTRY_CHARS / 3);
    writeCachedTranscript("chat-a", [makeEvent("a", big), makeEvent("b", big), makeEvent("c", big)], 0, 3);
    expect(readCachedTranscript("chat-a")?.events.map((event) => event.event_id)).toEqual(["c"]);

    writeCachedTranscript("chat-a", [makeEvent("huge", "x".repeat(MAX_ENTRY_CHARS))], 0, 1);
    expect(readCachedTranscript("chat-a")).toBeNull();
  });

  it("holds at most MAX_CACHED_CHATS chats, dropping the one written longest ago", () => {
    for (let index = 0; index <= MAX_CACHED_CHATS; index++) {
      writeCachedTranscript(`chat-${index}`, makeEvents(1), 0, 1);
    }

    expect(readCachedTranscript("chat-0")).toBeNull();
    expect(readCachedTranscript("chat-1")).not.toBeNull();
    expect(readCachedTranscript(`chat-${MAX_CACHED_CHATS}`)).not.toBeNull();
  });

  it("counts a rewrite as the newest, so the chat survives later writes", () => {
    for (let index = 0; index < MAX_CACHED_CHATS; index++) {
      writeCachedTranscript(`chat-${index}`, makeEvents(1), 0, 1);
    }
    writeCachedTranscript("chat-0", makeEvents(1), 0, 1);
    writeCachedTranscript("chat-new", makeEvents(1), 0, 1);

    expect(readCachedTranscript("chat-0")).not.toBeNull();
    expect(readCachedTranscript("chat-1")).toBeNull();
  });

  it("makes room over quota by dropping the oldest other chats", () => {
    const content = "y".repeat(1000);
    vi.stubGlobal("sessionStorage", memoryStorage(2600));
    writeCachedTranscript("chat-old", [makeEvent("a", content)], 0, 1);
    writeCachedTranscript("chat-mid", [makeEvent("b", content)], 0, 1);
    writeCachedTranscript("chat-new", [makeEvent("c", content)], 0, 1);

    expect(readCachedTranscript("chat-old")).toBeNull();
    expect(readCachedTranscript("chat-mid")).not.toBeNull();
    expect(readCachedTranscript("chat-new")).not.toBeNull();
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
  });

  it("does nothing where there is no session storage", () => {
    vi.unstubAllGlobals();
    vi.stubGlobal("sessionStorage", undefined);

    writeCachedTranscript("chat-a", makeEvents(2), 0, 2);

    expect(readCachedTranscript("chat-a")).toBeNull();
  });
});
