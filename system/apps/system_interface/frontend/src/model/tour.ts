/**
 * The show-around tour (the new-onboarding spec, sections 2.6 and 3.4): a pure state machine over the cards, and
 * the controller that runs it in the shell. The tour begins the first time the workspace's agent goes to work, so
 * the user learns the product while they wait for a reply (after ``startAfterWorkingMs`` of work, for a start on
 * the first message or only on a task that is taking a while); the agent answering interrupts it, so that Next
 * then shows a pause notice and the tour resumes from the card that would have come next the next time the agent
 * works. The controller reads and writes the workspace's tour record through ``/api/tour``, runs the tour again
 * from the launcher's "Show me around" row, does a card's action as its Next is pressed, and tells the Imbue Studio
 * chrome which of its controls to ring while a card is about one.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { postJson, requestJson } from "@imbue/workspace-ui/src/models/http";
import type { ChromeAnchor, TourAction, TourCard } from "./tourCards";
import { TOUR_CARDS } from "./tourCards";

export type TourState =
  | { readonly phase: "idle" }
  | { readonly phase: "card"; readonly index: number; readonly isInterrupted: boolean }
  | { readonly phase: "paused"; readonly resumeIndex: number }
  | { readonly phase: "exit-badge" };

export const IDLE_TOUR: TourState = { phase: "idle" };

/** How long the agent has to have been working before the tour begins: 0 starts it on the first message, a longer
 *  wait starts it only on a task that is taking a while. Overridden per visit by the ``tour_start_ms`` query
 *  parameter, to try both. */
export const TOUR_START_AFTER_WORKING_MS = 0;
export const TOUR_START_QUERY_PARAM = "tour_start_ms";

/** The wait before the tour begins for a page opened with ``search``: the query parameter when it is a whole
 *  number of milliseconds, else the default. */
export function tourStartDelayFromSearch(search: string): number {
  const raw = new URLSearchParams(search).get(TOUR_START_QUERY_PARAM);
  if (raw === null || !/^\d+$/.test(raw)) return TOUR_START_AFTER_WORKING_MS;
  return Number(raw);
}

export function startTour(index = 0): TourState {
  return { phase: "card", index, isInterrupted: false };
}

/** Forward: the next card, or the end after the last; after an interruption, the pause notice instead, holding
 *  the card that would have come next. */
export function nextCard(state: TourState, cards: readonly TourCard[] = TOUR_CARDS): TourState {
  if (state.phase !== "card") return state;
  const next = state.index + 1;
  if (next >= cards.length) return IDLE_TOUR;
  return state.isInterrupted ? { phase: "paused", resumeIndex: next } : startTour(next);
}

export function previousCard(state: TourState): TourState {
  if (state.phase !== "card" || state.index === 0) return state;
  return { phase: "card", index: state.index - 1, isInterrupted: state.isInterrupted };
}

/** The agent answered while a card was up: the card stays, and the next Next pauses the tour. */
export function interruptTour(state: TourState): TourState {
  return state.phase === "card" ? { ...state, isInterrupted: true } : state;
}

/** Leaving early shows the exit badge; "Done" on the last card goes through ``nextCard`` and shows none. */
export function exitTour(state: TourState): TourState {
  return state.phase === "card" ? { phase: "exit-badge" } : IDLE_TOUR;
}

export function dismissBadge(state: TourState): TourState {
  return state.phase === "exit-badge" ? IDLE_TOUR : state;
}

export function dismissPause(state: TourState): TourState {
  return state.phase === "paused" ? IDLE_TOUR : state;
}

export function cardShown(state: TourState, cards: readonly TourCard[] = TOUR_CARDS): TourCard | null {
  return state.phase === "card" ? cards[state.index] : null;
}

/** The chrome control the shown card is about, or null (no card, or a card about the shell's own things). */
export function chromeAnchorShown(state: TourState, cards: readonly TourCard[] = TOUR_CARDS): ChromeAnchor | null {
  return cardShown(state, cards)?.chromeAnchor ?? null;
}

/** What the workspace remembers of the tour (``tour.json``): whether it has started, and the card it resumes at. */
export interface TourRecord {
  readonly isSeen: boolean;
  readonly resumeIndex: number | null;
}

export const UNSEEN_TOUR: TourRecord = { isSeen: false, resumeIndex: null };

/** The routes the controller calls, injectable so it is tested against a fake. */
export interface TourApi {
  fetch(): Promise<TourRecord>;
  write(record: TourRecord): Promise<void>;
}

interface TourRecordWire {
  readonly is_seen?: unknown;
  readonly resume_index?: unknown;
}

export function parseTourRecord(body: TourRecordWire): TourRecord {
  const resumeIndex = body.resume_index;
  return {
    isSeen: body.is_seen === true,
    resumeIndex:
      typeof resumeIndex === "number" && Number.isInteger(resumeIndex) && resumeIndex >= 0 ? resumeIndex : null,
  };
}

export async function fetchTourRecord(): Promise<TourRecord> {
  return parseTourRecord(await requestJson<TourRecordWire>("GET", apiUrl("/api/tour")));
}

export async function writeTourRecord(record: TourRecord): Promise<void> {
  await postJson<unknown>(apiUrl("/api/tour"), { is_seen: record.isSeen, resume_index: record.resumeIndex });
}

export const TOUR_API: TourApi = { fetch: fetchTourRecord, write: writeTourRecord };

export interface TourControllerDependencies {
  readonly api: TourApi;
  /** Ask the Imbue Studio chrome to ring one of its controls, or clear the ring. */
  readonly highlightChrome: (anchor: ChromeAnchor | null) => void;
  /** Whether the tour may begin by itself right now: the desktop layout, no solo window, the owner looking. */
  readonly isAutoStartAllowed: () => boolean;
  readonly redraw: () => void;
  /** Do what a card's Next does in the shell (snap the chat); a controller without one skips the actions. */
  readonly performAction?: (action: TourAction) => void;
  readonly schedule?: (callback: () => void, delayMs: number) => void;
  readonly cards?: readonly TourCard[];
  readonly startAfterWorkingMs?: number;
}

export class TourController {
  private state: TourState = IDLE_TOUR;
  private lastAnchor: ChromeAnchor | null = null;
  /** The workspace's record once read; null until then, so nothing starts by itself before it is known. */
  private record: TourRecord | null = null;
  private isAgentWorking = false;
  /** Bumped whenever a scheduled start is no longer wanted, so a stale callback does nothing. */
  private startGeneration = 0;
  private readonly cards: readonly TourCard[];
  private readonly schedule: (callback: () => void, delayMs: number) => void;
  private readonly startAfterWorkingMs: number;

  constructor(private readonly deps: TourControllerDependencies) {
    this.cards = deps.cards ?? TOUR_CARDS;
    this.schedule = deps.schedule ?? ((callback, delayMs) => setTimeout(callback, delayMs));
    this.startAfterWorkingMs = deps.startAfterWorkingMs ?? TOUR_START_AFTER_WORKING_MS;
  }

  getState(): TourState {
    return this.state;
  }

  getCards(): readonly TourCard[] {
    return this.cards;
  }

  /** The card on screen, or null. */
  shownCard(): TourCard | null {
    return cardShown(this.state, this.cards);
  }

  /** Read what the workspace remembers, so the tour can begin or resume once the agent works. A read that fails
   *  leaves nothing to start by itself; the launcher row still works. */
  async load(): Promise<void> {
    let fetched: TourRecord;
    try {
      fetched = await this.deps.api.fetch();
    } catch (error) {
      console.warn("[si-tour] could not read the tour record", error);
      return;
    }
    // A record written while the read was out (the launcher's start) is newer than what the read answers.
    if (this.record === null) this.record = fetched;
    if (this.isAgentWorking) this.considerStart();
  }

  /** What the avatar says: the agent went to work (the tour may begin or resume) or stopped (a card on screen is
   *  interrupted, and a start not yet begun is dropped). */
  noteAgentWorking(isWorking: boolean): void {
    if (isWorking === this.isAgentWorking) return;
    this.isAgentWorking = isWorking;
    if (isWorking) {
      this.considerStart();
      return;
    }
    this.startGeneration += 1;
    if (this.state.phase === "card") this.apply(interruptTour(this.state));
  }

  /** Begin after the configured stretch of work, from the card that waits to resume, or the first on a workspace
   *  where the tour has never run; nothing when the record is unknown or says there is nothing to start, or when
   *  the card to resume at is past the list (a record from a build with more cards). */
  private considerStart(): void {
    if (this.record === null || this.state.phase !== "idle") return;
    const index = this.record.resumeIndex ?? (this.record.isSeen ? null : 0);
    if (index === null || index >= this.cards.length) return;
    const generation = (this.startGeneration += 1);
    this.schedule(() => {
      if (generation !== this.startGeneration || !this.isAgentWorking || this.state.phase !== "idle") return;
      if (!this.deps.isAutoStartAllowed()) return;
      this.begin(index);
    }, this.startAfterWorkingMs);
  }

  /** Start from the first card, whether or not the tour has run before (the launcher's row). */
  start(): void {
    this.startGeneration += 1;
    this.begin(0);
  }

  private begin(index: number): void {
    this.apply(startTour(index));
    this.persist({ isSeen: true, resumeIndex: null });
  }

  next(): void {
    const action = this.shownCard()?.action ?? null;
    if (action !== null) this.deps.performAction?.(action);
    const next = nextCard(this.state, this.cards);
    this.apply(next);
    if (next.phase === "paused") this.persist({ isSeen: true, resumeIndex: next.resumeIndex });
  }

  back(): void {
    this.apply(previousCard(this.state));
  }

  exit(): void {
    this.apply(exitTour(this.state));
  }

  dismissBadge(): void {
    this.apply(dismissBadge(this.state));
  }

  /** The pause notice acknowledged: the tour resumes the next time the agent works, or right away when it is. */
  dismissPause(): void {
    this.apply(dismissPause(this.state));
    if (this.isAgentWorking) this.considerStart();
  }

  private persist(record: TourRecord): void {
    if (
      this.record !== null &&
      this.record.isSeen === record.isSeen &&
      this.record.resumeIndex === record.resumeIndex
    ) {
      return;
    }
    this.record = record;
    void this.deps.api.write(record).catch((error: unknown) => {
      console.warn("[si-tour] could not record the tour's progress", error);
    });
  }

  private apply(next: TourState): void {
    this.state = next;
    const anchor = chromeAnchorShown(next, this.cards);
    if (anchor !== this.lastAnchor) {
      this.lastAnchor = anchor;
      this.deps.highlightChrome(anchor);
    }
    this.deps.redraw();
  }
}
