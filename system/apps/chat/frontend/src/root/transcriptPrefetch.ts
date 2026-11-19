/**
 * The chat root starts reading a chat's newest events as it creates the chat's frame, and hands the read to the page
 * when the page loads (``window.chatRootEmbed``, ``embedApi.ts``).
 *
 * A page asks for its transcript only once its document and bundle have loaded and run, so on its own the read
 * starts a round trip or two after the click; started here it runs beside the page's load, and a stopped chat's
 * transcript is parsed on the server meanwhile. The page takes it once, as its first load; anything written after
 * this read is caught up by the page once its stream is open (``catchUpLiveTail``).
 */

import type { EventsResponse } from "../models/Response";

export class TranscriptPrefetch {
  private readonly pending = new Map<string, Promise<EventsResponse>>();

  constructor(
    private readonly read: (chatId: string) => Promise<EventsResponse>,
    // Only a chat with a transcript to read: a chat not listed yet (provisional, awaiting its first send) has none.
    private readonly isReadable: (chatId: string) => boolean,
  ) {}

  /** Start reading ``chatId``'s newest events, unless a read of it is already waiting for its page. */
  start(chatId: string): void {
    if (this.pending.has(chatId) || !this.isReadable(chatId)) return;
    const read = this.read(chatId);
    // A failed read is the page's to retry and report; unclaimed, it must not surface as an unhandled rejection.
    read.catch(() => undefined);
    this.pending.set(chatId, read);
  }

  /** The read started for ``chatId``, once: the page's first load takes it, and later ones read for themselves. */
  take(chatId: string): Promise<EventsResponse> | null {
    const read = this.pending.get(chatId) ?? null;
    this.pending.delete(chatId);
    return read;
  }

  /** Forget ``chatId``'s read (its frame is gone before its page took it). */
  drop(chatId: string): void {
    this.pending.delete(chatId);
  }
}
