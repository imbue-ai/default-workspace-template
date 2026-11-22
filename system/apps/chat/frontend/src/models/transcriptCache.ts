/**
 * What each chat last showed at the bottom of its page, so a chat's page paints the conversation
 * the moment it opens instead of waiting on its transcript request. Every chat in the root is its
 * own page, and only a few are kept alive (``root/framePool.ts``), so opening any other chat starts
 * a page with nothing in memory; this is what it shows until the request answers and replaces it.
 *
 * A snapshot holds only what fills the chat's screen (plus a margin), sized by the text it renders
 * rather than by a count of events, so one long message cannot make it large. All snapshots share
 * one budget, ``TOTAL_BUDGET_CHARS``; past it the chat viewed longest ago goes first.
 *
 * Kept in sessionStorage, which the root and its pages share (same origin, same window), and not
 * localStorage: a transcript can hold anything the user pasted, and it should not outlive the
 * session on disk. Each window therefore has its own snapshots and its own budget. Best-effort
 * throughout -- a missing, corrupt or over-quota entry only means the page waits for its request
 * as it would without this.
 */

import type { TranscriptEvent } from "./Response";

const ENTRY_KEY_PREFIX = "transcript-cache:";
const INDEX_KEY = "transcript-cache-index";
const CACHE_VERSION = 3;

// The characters all of a window's snapshots may take together. Chromium keeps sessionStorage twice
// (the browser process and the page's renderer, two bytes a character each), so this is about 1 MB
// of memory per window.
export const TOTAL_BUDGET_CHARS = 250_000;
// The most one chat's snapshot may take, whatever its screen holds.
export const MAX_ENTRY_CHARS = 32_000;
// How much more than a screen a snapshot keeps, so a small scroll or a taller window still finds
// the conversation drawn.
export const SCREEN_MARGIN = 1.25;
// The most events one snapshot keeps, however little they draw: a long run of events that draw
// nothing (hidden calls, results) would otherwise pull the whole held window in.
export const MAX_SNAPSHOT_EVENTS = 200;
// A row's chrome (a bubble's padding, a chip, a header) counted as this many characters of text.
export const ROW_CHROME_CHARS = 60;

/** How much rendered text the chat's screen holds: its height, and the text a pixel of it shows. */
export interface ScreenMeasure {
  readonly heightPx: number;
  /** Characters of rendered text per pixel of transcript height; null when there was too little drawn to tell. */
  readonly charsPerPx: number | null;
  /** The height of what the page draws under the transcript's last row (the turn's done mark, its status row). */
  readonly trailingPx: number;
}

// Used until the page has measured its own screen (a page that has not been shown yet).
export const DEFAULT_SCREEN: ScreenMeasure = { heightPx: 1000, charsPerPx: null, trailingPx: 0 };
// Characters per pixel of a typical transcript, for a screen that has not measured its own.
export const DEFAULT_CHARS_PER_PX = 3;

let screenMeasurer: () => ScreenMeasure | null = () => null;
let lastScreen: ScreenMeasure = DEFAULT_SCREEN;

/** How the page measures its chat's screen; the measurer answers null while the screen cannot be measured (hidden). */
export function setScreenMeasurer(measurer: () => ScreenMeasure | null): void {
  screenMeasurer = measurer;
}

/** The chat's screen as measured now, or as last measured while it could be. */
export function currentScreen(): ScreenMeasure {
  const measured = screenMeasurer();
  if (measured !== null) lastScreen = measured;
  return lastScreen;
}

export interface CachedTranscript {
  readonly events: TranscriptEvent[];
  readonly offset: number;
  readonly total: number;
  readonly trailingPx: number;
}

interface StoredEntry {
  version: number;
  events: TranscriptEvent[];
  offset: number;
  total: number;
  trailingPx: number;
}

/** One chat in the index: its id and the characters its entry takes. */
interface IndexEntry {
  id: string;
  chars: number;
}

/** The session's storage, or null where there is none (a non-browser environment). */
function sessionStore(): Storage | null {
  return typeof sessionStorage === "undefined" ? null : sessionStorage;
}

function entryKey(chatId: string): string {
  return `${ENTRY_KEY_PREFIX}${chatId}`;
}

/** The cached chats, most recently viewed first. */
function readIndex(storage: Storage): IndexEntry[] {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(INDEX_KEY) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (entry): entry is IndexEntry =>
        typeof entry === "object" && entry !== null && typeof entry.id === "string" && typeof entry.chars === "number",
    );
  } catch {
    return [];
  }
}

function writeIndex(storage: Storage, index: IndexEntry[]): void {
  storage.setItem(INDEX_KEY, JSON.stringify(index));
}

/** The characters of text ``event`` renders, its row's chrome included. */
export function renderedChars(event: TranscriptEvent): number {
  switch (event.type) {
    case "user_message":
      return event.display === "hidden" ? 0 : (event.display_body ?? event.content).length + ROW_CHROME_CHARS;
    case "assistant_message": {
      let chars = event.text.length > 0 ? event.text.length + ROW_CHROME_CHARS : 0;
      for (const call of event.tool_calls) {
        if (call.display !== "hidden") chars += ROW_CHROME_CHARS;
      }
      return chars;
    }
    case "tool_result":
      return 0;
    default:
      return ROW_CHROME_CHARS;
  }
}

/** The characters of rendered text that fill ``screen`` with its margin. */
export function screenChars(screen: ScreenMeasure): number {
  return Math.ceil(screen.heightPx * (screen.charsPerPx ?? DEFAULT_CHARS_PER_PX) * SCREEN_MARGIN);
}

/**
 * The last ``keep`` characters of ``text``, from the start of a line where one is near, reopening a
 * code fence the cut fell inside so the rest still renders as code.
 */
export function textTail(text: string, keep: number): string {
  if (text.length <= keep) return text;
  let cut = text.length - keep;
  const lineStart = text.indexOf("\n", cut);
  if (lineStart !== -1 && lineStart - cut < 200) cut = lineStart + 1;
  const dropped = text.slice(0, cut);
  const fencesDropped = dropped.split("\n").filter((line) => line.trimStart().startsWith("```")).length;
  return (fencesDropped % 2 === 1 ? "```\n" : "") + text.slice(cut);
}

/** ``event`` with each text it renders cut to its last ``keep`` characters. */
function withTextTail(event: TranscriptEvent, keep: number): TranscriptEvent {
  switch (event.type) {
    case "user_message":
      return {
        ...event,
        content: textTail(event.content, keep),
        ...(event.display_body === undefined ? {} : { display_body: textTail(event.display_body, keep) }),
      };
    case "assistant_message":
      return { ...event, text: textTail(event.text, keep) };
    default:
      return event;
  }
}

/**
 * The tail of ``events`` that fills ``screen``: the last events whose rendered text adds up to a
 * screen and its margin, the earliest of them cut to the part that shows, and no text longer than a
 * screen. ``offset`` is the global index of the first kept event.
 */
export function tailForScreen(
  events: TranscriptEvent[],
  firstOffset: number,
  screen: ScreenMeasure,
): { events: TranscriptEvent[]; offset: number } {
  const budget = screenChars(screen);
  let start = events.length;
  let filled = 0;
  while (start > 0 && filled < budget && events.length - start < MAX_SNAPSHOT_EVENTS) {
    start -= 1;
    filled += renderedChars(events[start]);
  }
  const tail = events.slice(start).map((event, index) => {
    // The first kept event shows only as much as the screen has left for it.
    const room = index === 0 ? budget - (filled - renderedChars(event)) : budget;
    return withTextTail(event, Math.max(room, ROW_CHROME_CHARS));
  });
  return { events: tail, offset: firstOffset + start };
}

/** The serialized entry for the tail, dropping its earliest events past ``MAX_ENTRY_CHARS``; null when the last alone is over. */
function encodeEntry(events: TranscriptEvent[], offset: number, total: number, trailingPx: number): string | null {
  let skip = 0;
  while (skip < events.length) {
    const entry: StoredEntry = {
      version: CACHE_VERSION,
      events: events.slice(skip),
      offset: offset + skip,
      total,
      trailingPx,
    };
    const encoded = JSON.stringify(entry);
    if (encoded.length <= MAX_ENTRY_CHARS) return encoded;
    // Drop the share of the kept events the excess amounts to, so a large tail takes a few passes, not one per event.
    const kept = events.length - skip;
    skip += Math.max(1, Math.floor(kept * (1 - MAX_ENTRY_CHARS / encoded.length)));
  }
  return null;
}

/** The chat's cached snapshot, or null when there is none (or it cannot be read). */
export function readCachedTranscript(chatId: string): CachedTranscript | null {
  const storage = sessionStore();
  if (storage === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(storage.getItem(entryKey(chatId)) ?? "null");
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const entry = parsed as Partial<StoredEntry>;
  if (
    entry.version !== CACHE_VERSION ||
    !Array.isArray(entry.events) ||
    entry.events.length === 0 ||
    typeof entry.offset !== "number" ||
    typeof entry.total !== "number"
  ) {
    return null;
  }
  return {
    events: entry.events,
    offset: entry.offset,
    total: entry.total,
    trailingPx: typeof entry.trailingPx === "number" ? entry.trailingPx : 0,
  };
}

/** Drop chats from the end of ``index`` (the least recently viewed) until ``room`` more characters fit the budget. */
function evictForRoom(storage: Storage, index: IndexEntry[], room: number): void {
  let used = index.reduce((sum, entry) => sum + entry.chars, 0);
  while (index.length > 0 && used + room > TOTAL_BUDGET_CHARS) {
    const evicted = index.pop()!;
    storage.removeItem(entryKey(evicted.id));
    used -= evicted.chars;
  }
}

/**
 * Remember the bottom of a window that reaches the transcript's live end, sized to ``screen``. A
 * chat already cached keeps its place in the viewing order (a chat streaming behind another is not
 * being viewed); a new one counts as just viewed.
 */
export function writeCachedTranscript(
  chatId: string,
  events: TranscriptEvent[],
  firstOffset: number,
  total: number,
  screen: ScreenMeasure = DEFAULT_SCREEN,
): void {
  const storage = sessionStore();
  if (storage === null) return;
  try {
    const tail = tailForScreen(events, firstOffset, screen);
    const encoded = encodeEntry(tail.events, tail.offset, total, screen.trailingPx);
    const index = readIndex(storage);
    const position = index.findIndex((entry) => entry.id === chatId);
    const others = index.filter((entry) => entry.id !== chatId);
    // Room is made from the chats viewed longer ago than this one, never from those viewed since.
    const newer = position === -1 ? [] : others.slice(0, position);
    const older = position === -1 ? others : others.slice(position);
    const newerChars = newer.reduce((sum, entry) => sum + entry.chars, 0);
    // A chat that the ones viewed since already crowd out of the budget is itself the one to go.
    if (encoded === null || newerChars + encoded.length > TOTAL_BUDGET_CHARS) {
      storage.removeItem(entryKey(chatId));
      writeIndex(storage, others);
      return;
    }
    evictForRoom(storage, older, encoded.length + newerChars);
    for (;;) {
      try {
        storage.setItem(entryKey(chatId), encoded);
        break;
      } catch (error) {
        // Over quota: make room by dropping the least recently viewed other chat, until none is left.
        const evicted = older.pop() ?? newer.pop();
        if (evicted === undefined) throw error;
        storage.removeItem(entryKey(evicted.id));
      }
    }
    const written = { id: chatId, chars: encoded.length };
    writeIndex(storage, position === -1 ? [written, ...newer, ...older] : [...newer, written, ...older]);
  } catch (error) {
    console.warn(`Could not cache the transcript of chat ${chatId}`, error);
  }
}

/** The chat is being viewed: it becomes the last one evicted. */
export function touchCachedTranscript(chatId: string): void {
  const storage = sessionStore();
  if (storage === null) return;
  try {
    const index = readIndex(storage);
    const position = index.findIndex((entry) => entry.id === chatId);
    if (position <= 0) return;
    writeIndex(storage, [index[position], ...index.slice(0, position), ...index.slice(position + 1)]);
  } catch {
    // Storage refused (privacy mode): nothing was cached either.
  }
}

/** Forget a chat's cached snapshot. */
export function dropCachedTranscript(chatId: string): void {
  try {
    sessionStore()?.removeItem(entryKey(chatId));
  } catch {
    // Storage refused (privacy mode): nothing was cached either.
  }
  pruneCachedTranscripts((cachedId) => cachedId !== chatId);
}

/** Forget every cached snapshot whose chat ``isKept`` rejects (chats deleted, here or elsewhere). */
export function pruneCachedTranscripts(isKept: (chatId: string) => boolean): void {
  const storage = sessionStore();
  if (storage === null) return;
  try {
    const index = readIndex(storage);
    const kept = index.filter((entry) => isKept(entry.id));
    if (kept.length === index.length) return;
    for (const entry of index) {
      if (!isKept(entry.id)) storage.removeItem(entryKey(entry.id));
    }
    writeIndex(storage, kept);
  } catch {
    // Storage refused (privacy mode): nothing was cached either.
  }
}
