import { describe, expect, it } from "vitest";
import {
  IDLE_TOUR,
  TOUR_START_AFTER_WORKING_MS,
  TourController,
  cardShown,
  chromeAnchorShown,
  dismissBadge,
  dismissPause,
  exitTour,
  interruptTour,
  nextCard,
  parseTourRecord,
  previousCard,
  startTour,
  tourStartDelayFromSearch,
} from "./tour";
import type { TourAction } from "./tourCards";
import { TOUR_CARDS } from "./tourCards";
import { FAKE_TOUR_CARDS as CARDS, FakeTourApi } from "../testing/fakeTour";

describe("the tour's state machine", () => {
  it("walks the cards forward and back, ends on Done with no badge, and leaves a badge on an early exit", () => {
    let state = startTour();
    expect(cardShown(state, CARDS)?.id).toBe("a");
    expect(previousCard(state)).toBe(state);
    state = nextCard(state, CARDS);
    expect(cardShown(state, CARDS)?.id).toBe("b");
    expect(chromeAnchorShown(state, CARDS)).toBe("share");
    state = previousCard(state);
    expect(cardShown(state, CARDS)?.id).toBe("a");
    expect(chromeAnchorShown(state, CARDS)).toBeNull();
    state = nextCard(nextCard(state, CARDS), CARDS);
    expect(cardShown(state, CARDS)?.id).toBe("c");
    expect(nextCard(state, CARDS)).toEqual(IDLE_TOUR);
    expect(exitTour(state)).toEqual({ phase: "exit-badge" });
    expect(cardShown(exitTour(state), CARDS)).toBeNull();
    expect(dismissBadge(exitTour(state))).toEqual(IDLE_TOUR);
    expect(exitTour(IDLE_TOUR)).toEqual(IDLE_TOUR);
    expect(nextCard(IDLE_TOUR, CARDS)).toEqual(IDLE_TOUR);
  });

  it("pauses on the Next after an interruption, holding the card that would have come next, and not after the last card", () => {
    const interrupted = interruptTour(startTour(1));
    expect(cardShown(interrupted, CARDS)?.id).toBe("b");
    expect(previousCard(interrupted)).toEqual({ phase: "card", index: 0, isInterrupted: true });
    expect(nextCard(interrupted, CARDS)).toEqual({ phase: "paused", resumeIndex: 2 });
    expect(cardShown(nextCard(interrupted, CARDS), CARDS)).toBeNull();
    expect(dismissPause(nextCard(interrupted, CARDS))).toEqual(IDLE_TOUR);
    expect(nextCard(interruptTour(startTour(2)), CARDS)).toEqual(IDLE_TOUR);
    expect(interruptTour(IDLE_TOUR)).toEqual(IDLE_TOUR);
    expect(dismissPause(IDLE_TOUR)).toEqual(IDLE_TOUR);
  });

  it("ships thirteen cards, each with words, ending on a Done card at the launcher", () => {
    expect(TOUR_CARDS).toHaveLength(13);
    expect(new Set(TOUR_CARDS.map((card) => card.id)).size).toBe(TOUR_CARDS.length);
    for (const card of TOUR_CARDS) expect(card.text.length).toBeGreaterThan(0);
    const last = TOUR_CARDS[TOUR_CARDS.length - 1];
    expect(last.target).toBe("launcher");
    expect(last.buttonLabel).toBe("Done");
    expect(TOUR_CARDS.filter((card) => card.image !== null).map((card) => card.id)).toEqual([
      "connect",
      "devices",
      "providers",
    ]);
    // The chat card comes before the apps card: its Next snaps the chat aside, which is what shows the shortcuts.
    const ids = TOUR_CARDS.map((card) => card.id);
    expect(TOUR_CARDS.find((card) => card.action === "snap-avatar-window-right")?.id).toBe("chat-window");
    expect(ids.indexOf("chat-window")).toBeLessThan(ids.indexOf("apps"));
    expect(TOUR_CARDS.find((card) => card.id === "connect")?.bullets).toHaveLength(2);
  });

  it("reads the record's fields tolerantly, and the start delay from the query string", () => {
    expect(parseTourRecord({ is_seen: true, resume_index: 3 })).toEqual({ isSeen: true, resumeIndex: 3 });
    expect(parseTourRecord({ is_seen: "yes", resume_index: -1 })).toEqual({ isSeen: false, resumeIndex: null });
    expect(parseTourRecord({})).toEqual({ isSeen: false, resumeIndex: null });
    expect(tourStartDelayFromSearch("")).toBe(TOUR_START_AFTER_WORKING_MS);
    expect(tourStartDelayFromSearch("?tour_start_ms=15000")).toBe(15000);
    expect(tourStartDelayFromSearch("?tour_start_ms=soon")).toBe(TOUR_START_AFTER_WORKING_MS);
  });
});

function controller(api: FakeTourApi, options: { isAllowed?: boolean; startAfterWorkingMs?: number } = {}) {
  const highlights: (string | null)[] = [];
  const actions: TourAction[] = [];
  const scheduled: { callback: () => void; delayMs: number }[] = [];
  let redraws = 0;
  const tour = new TourController({
    api,
    highlightChrome: (anchor) => highlights.push(anchor),
    isAutoStartAllowed: () => options.isAllowed ?? true,
    performAction: (action) => actions.push(action),
    redraw: () => {
      redraws += 1;
    },
    schedule: (callback, delayMs) => scheduled.push({ callback, delayMs }),
    cards: CARDS,
    startAfterWorkingMs: options.startAfterWorkingMs,
  });
  return { tour, highlights, actions, scheduled, redraws: () => redraws };
}

/** Run every start the controller scheduled and not yet run. */
function fire(scheduled: { callback: () => void }[]): void {
  const pending = scheduled.splice(0);
  for (const entry of pending) entry.callback();
}

describe("TourController", () => {
  it("begins on a fresh workspace once the agent has worked for the configured stretch, and records that it ran", async () => {
    const api = new FakeTourApi();
    const { tour, scheduled } = controller(api, { startAfterWorkingMs: 15000 });
    await tour.load();
    expect(scheduled).toEqual([]);
    tour.noteAgentWorking(true);
    expect(scheduled.map((entry) => entry.delayMs)).toEqual([15000]);
    expect(tour.getState()).toEqual(IDLE_TOUR);
    fire(scheduled);
    expect(tour.shownCard()?.id).toBe("a");
    await Promise.resolve();
    expect(api.writes).toEqual([{ isSeen: true, resumeIndex: null }]);
  });

  it("drops a start the agent finished before, and begins when it works again", async () => {
    const api = new FakeTourApi();
    const { tour, scheduled } = controller(api, { startAfterWorkingMs: 15000 });
    await tour.load();
    tour.noteAgentWorking(true);
    tour.noteAgentWorking(false);
    fire(scheduled);
    expect(tour.getState()).toEqual(IDLE_TOUR);
    tour.noteAgentWorking(true);
    fire(scheduled);
    expect(tour.shownCard()?.id).toBe("a");
  });

  it("begins at once when the agent was already working as the record arrived", async () => {
    const api = new FakeTourApi();
    const { tour, scheduled } = controller(api);
    tour.noteAgentWorking(true);
    expect(scheduled).toEqual([]);
    await tour.load();
    expect(scheduled.map((entry) => entry.delayMs)).toEqual([0]);
    fire(scheduled);
    expect(tour.shownCard()?.id).toBe("a");
  });

  it("starts nothing by itself once the tour has run, when the read fails, or when the moment is wrong", async () => {
    const seen = new FakeTourApi({ isSeen: true });
    const { tour: seenTour, scheduled: seenScheduled } = controller(seen);
    await seenTour.load();
    seenTour.noteAgentWorking(true);
    expect(seenScheduled).toEqual([]);

    const failing = new FakeTourApi();
    failing.isFetchFailing = true;
    const { tour: failingTour, scheduled: failingScheduled } = controller(failing);
    await failingTour.load();
    failingTour.noteAgentWorking(true);
    expect(failingScheduled).toEqual([]);

    const { tour: phoneTour, scheduled: phoneScheduled } = controller(new FakeTourApi(), { isAllowed: false });
    await phoneTour.load();
    phoneTour.noteAgentWorking(true);
    fire(phoneScheduled);
    expect(phoneTour.getState()).toEqual(IDLE_TOUR);
  });

  it("is interrupted by the agent's reply: Next then pauses, remembering the next card, which the next work resumes", async () => {
    const api = new FakeTourApi();
    const { tour, scheduled, highlights } = controller(api);
    await tour.load();
    tour.noteAgentWorking(true);
    fire(scheduled);
    tour.next();
    expect(tour.shownCard()?.id).toBe("b");
    tour.noteAgentWorking(false);
    // The card stays up; the interruption shows on the next Next.
    expect(tour.shownCard()?.id).toBe("b");
    tour.next();
    expect(tour.getState()).toEqual({ phase: "paused", resumeIndex: 2 });
    expect(highlights).toEqual(["share", null]);
    await Promise.resolve();
    expect(api.writes).toEqual([
      { isSeen: true, resumeIndex: null },
      { isSeen: true, resumeIndex: 2 },
    ]);
    tour.dismissPause();
    expect(tour.getState()).toEqual(IDLE_TOUR);
    tour.noteAgentWorking(true);
    fire(scheduled);
    expect(tour.shownCard()?.id).toBe("c");
    await Promise.resolve();
    expect(api.writes[2]).toEqual({ isSeen: true, resumeIndex: null });
  });

  it("resumes at once when the pause is acknowledged while the agent is already working again", async () => {
    const api = new FakeTourApi();
    const { tour, scheduled } = controller(api);
    await tour.load();
    tour.noteAgentWorking(true);
    fire(scheduled);
    tour.noteAgentWorking(false);
    tour.next();
    tour.noteAgentWorking(true);
    expect(tour.getState()).toEqual({ phase: "paused", resumeIndex: 1 });
    tour.dismissPause();
    fire(scheduled);
    expect(tour.shownCard()?.id).toBe("b");
  });

  it("resumes from the record's card on a later visit, and a pause after the last card ends the tour", async () => {
    const api = new FakeTourApi({ isSeen: true, resumeIndex: 2 });
    const { tour, scheduled } = controller(api);
    await tour.load();
    tour.noteAgentWorking(true);
    fire(scheduled);
    expect(tour.shownCard()?.id).toBe("c");
    tour.noteAgentWorking(false);
    tour.next();
    expect(tour.getState()).toEqual(IDLE_TOUR);
    await Promise.resolve();
    expect(api.writes).toEqual([{ isSeen: true, resumeIndex: null }]);
  });

  it("keeps what the launcher's start recorded when the read answers with an older record after it", async () => {
    const api = new FakeTourApi();
    api.isFetchDeferred = true;
    const { tour, scheduled } = controller(api);
    const loading = tour.load();
    tour.start();
    tour.next();
    tour.next();
    tour.next();
    expect(tour.getState()).toEqual(IDLE_TOUR);
    api.resolveFetch();
    await loading;
    // The read's unseen record is stale: the tour has run, so the agent's work starts nothing.
    tour.noteAgentWorking(true);
    expect(scheduled).toEqual([]);
    expect(api.writes).toEqual([{ isSeen: true, resumeIndex: null }]);
  });

  it("resumes nothing from a record whose card is past the list, while the launcher still starts the tour", async () => {
    const api = new FakeTourApi({ isSeen: true, resumeIndex: CARDS.length });
    const { tour, scheduled } = controller(api);
    await tour.load();
    tour.noteAgentWorking(true);
    expect(scheduled).toEqual([]);
    expect(tour.getState()).toEqual(IDLE_TOUR);
    tour.start();
    expect(tour.shownCard()?.id).toBe("a");
  });

  it("does a card's action as its Next is pressed, and runs again from the launcher whatever the record says", async () => {
    const api = new FakeTourApi({ isSeen: true, resumeIndex: 1 });
    const { tour, highlights, actions, redraws } = controller(api);
    await tour.load();
    tour.start();
    expect(tour.shownCard()?.id).toBe("a");
    expect(highlights).toEqual([]);
    tour.next();
    expect(actions).toEqual(["snap-avatar-window-right"]);
    expect(highlights).toEqual(["share"]);
    tour.next();
    expect(actions).toEqual(["snap-avatar-window-right"]);
    expect(highlights).toEqual(["share", null]);
    tour.back();
    expect(highlights).toEqual(["share", null, "share"]);
    tour.exit();
    expect(tour.getState()).toEqual({ phase: "exit-badge" });
    expect(highlights).toEqual(["share", null, "share", null]);
    tour.dismissBadge();
    expect(tour.getState()).toEqual(IDLE_TOUR);
    expect(redraws()).toBe(6);
    await Promise.resolve();
    // The launcher's start clears the card that waited to resume.
    expect(api.writes).toEqual([{ isSeen: true, resumeIndex: null }]);
  });
});
