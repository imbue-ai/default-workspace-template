import { describe, expect, it } from "vitest";
import {
  IDLE_TOUR,
  TOUR_START_AFTER_WORKING_MS,
  TourController,
  cardShown,
  chromeAnchorShown,
  dismissBadge,
  dismissCoach,
  dismissPause,
  exitTour,
  interruptTour,
  nextCard,
  parseTourRecord,
  previousCard,
  showCoach,
  startTour,
  tourStartDelayFromSearch,
  UNSEEN_TOUR,
} from "./tour";
import type { TourRecord } from "./tour";
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
    expect(parseTourRecord({ is_seen: true, resume_index: 3 })).toEqual({
      isSeen: true,
      resumeIndex: 3,
      isFinished: false,
    });
    expect(parseTourRecord({ is_seen: true, resume_index: null, is_finished: true })).toEqual({
      isSeen: true,
      resumeIndex: null,
      isFinished: true,
    });
    expect(parseTourRecord({ is_seen: "yes", resume_index: -1, is_finished: "yes" })).toEqual(UNSEEN_TOUR);
    expect(parseTourRecord({})).toEqual(UNSEEN_TOUR);
    expect(tourStartDelayFromSearch("")).toBe(TOUR_START_AFTER_WORKING_MS);
    expect(tourStartDelayFromSearch("?tour_start_ms=15000")).toBe(15000);
    expect(tourStartDelayFromSearch("?tour_start_ms=soon")).toBe(TOUR_START_AFTER_WORKING_MS);
  });
});

/** A tour's record as the controller writes it: seen, with the card it resumes at and whether it is finished. */
function seenRecord(resumeIndex: number | null, isFinished: boolean): TourRecord {
  return { isSeen: true, resumeIndex, isFinished };
}

function controller(
  api: FakeTourApi,
  options: { isAllowed?: boolean; startAfterWorkingMs?: number; isViewerOwner?: () => boolean } = {},
) {
  const highlights: (string | null)[] = [];
  const actions: TourAction[] = [];
  const scheduled: { callback: () => void; delayMs: number }[] = [];
  let redraws = 0;
  const tour = new TourController({
    api,
    highlightChrome: (anchor) => highlights.push(anchor),
    isAutoStartAllowed: () => options.isAllowed ?? true,
    isViewerOwner: options.isViewerOwner ?? (() => true),
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
    expect(api.writes).toEqual([seenRecord(null, false)]);
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
    expect(api.writes).toEqual([seenRecord(null, false), seenRecord(2, false)]);
    tour.dismissPause();
    expect(tour.getState()).toEqual(IDLE_TOUR);
    tour.noteAgentWorking(true);
    fire(scheduled);
    expect(tour.shownCard()?.id).toBe("c");
    await Promise.resolve();
    expect(api.writes[2]).toEqual(seenRecord(null, false));
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
    expect(api.writes).toEqual([seenRecord(null, false), seenRecord(null, true)]);
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
    expect(api.writes).toEqual([seenRecord(null, false), seenRecord(null, true)]);
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
    // The launcher's start clears the card that waited to resume, and leaving the tour finishes it.
    expect(api.writes).toEqual([seenRecord(null, false), seenRecord(null, true)]);
  });
});

describe("whether the tour is finished", () => {
  it("is recorded when Done is pressed on the last card", async () => {
    const api = new FakeTourApi();
    const { tour } = controller(api);
    await tour.load();
    tour.start();
    tour.next();
    tour.next();
    expect(api.writes).toEqual([seenRecord(null, false)]);
    tour.next();
    expect(tour.getState()).toEqual(IDLE_TOUR);
    expect(api.writes).toEqual([seenRecord(null, false), seenRecord(null, true)]);
  });

  it("is recorded when the tour is left early", async () => {
    const api = new FakeTourApi();
    const { tour } = controller(api);
    await tour.load();
    tour.start();
    tour.exit();
    expect(api.writes).toEqual([seenRecord(null, false), seenRecord(null, true)]);
    // The badge's button changes nothing the record holds.
    tour.dismissBadge();
    expect(api.writes).toHaveLength(2);
  });

  it("is not recorded for a tour that waits to resume, on the pause or on a later load", async () => {
    const api = new FakeTourApi();
    const { tour, scheduled } = controller(api);
    await tour.load();
    tour.noteAgentWorking(true);
    fire(scheduled);
    tour.noteAgentWorking(false);
    tour.next();
    expect(tour.getState()).toEqual({ phase: "paused", resumeIndex: 1 });
    tour.dismissPause();
    expect(api.writes).toEqual([seenRecord(null, false), seenRecord(1, false)]);

    const { tour: laterVisit } = controller(api);
    await laterVisit.load();
    laterVisit.noteViewerChanged();
    expect(api.writes).toHaveLength(2);
    expect(api.record).toEqual(seenRecord(1, false));
  });

  it("is recorded by the owner's page for a seen record with nothing to resume: a tour a reload cut off, or one never offered", async () => {
    const api = new FakeTourApi({ isSeen: true });
    const { tour } = controller(api);
    await tour.load();
    expect(api.writes).toEqual([seenRecord(null, true)]);
    // Once is enough: nothing is written again for a record that already says so.
    tour.noteViewerChanged();
    const { tour: laterVisit } = controller(api);
    await laterVisit.load();
    expect(api.writes).toHaveLength(1);
  });

  it("is left alone by a visitor's page, and settled once the viewer turns out to be the owner", async () => {
    const api = new FakeTourApi({ isSeen: true });
    let isOwner = false;
    const { tour } = controller(api, { isViewerOwner: () => isOwner });
    await tour.load();
    tour.noteViewerChanged();
    expect(api.writes).toEqual([]);

    isOwner = true;
    tour.noteViewerChanged();
    expect(api.writes).toEqual([seenRecord(null, true)]);
  });

  it("is not settled under a tour that is on screen, nor for a workspace the tour is still to be offered in", async () => {
    const unseen = new FakeTourApi();
    const { tour: fresh } = controller(unseen);
    await fresh.load();
    expect(unseen.writes).toEqual([]);

    fresh.start();
    fresh.noteViewerChanged();
    expect(unseen.writes).toEqual([seenRecord(null, false)]);
  });

  it("stays recorded through every later write: the tour run again, and paused again", async () => {
    const api = new FakeTourApi({ isSeen: true, isFinished: true });
    const { tour, scheduled } = controller(api);
    await tour.load();
    tour.start();
    // The launcher's start changes nothing a finished record holds, so nothing is written for it.
    expect(api.writes).toEqual([]);
    tour.noteAgentWorking(true);
    tour.noteAgentWorking(false);
    tour.next();
    expect(tour.getState()).toEqual({ phase: "paused", resumeIndex: 1 });
    expect(api.writes).toEqual([seenRecord(1, true)]);
    tour.dismissPause();
    tour.noteAgentWorking(true);
    fire(scheduled);
    expect(tour.shownCard()?.id).toBe("b");
    expect(api.writes).toEqual([seenRecord(1, true), seenRecord(null, true)]);
  });
});

describe("a coach card", () => {
  it("takes an idle tour's place and goes back to idle, and leaves every other state as it is", () => {
    const coach = showCoach(IDLE_TOUR, "Your connections live here.", "permissions");
    expect(coach).toEqual({ phase: "coach", text: "Your connections live here.", chromeAnchor: "permissions" });
    expect(chromeAnchorShown(coach, CARDS)).toBe("permissions");
    expect(cardShown(coach, CARDS)).toBeNull();
    expect(dismissCoach(coach)).toEqual(IDLE_TOUR);

    const card = startTour();
    expect(showCoach(card, "Another.", "share")).toBe(card);
    expect(showCoach(coach, "Another.", "share")).toBe(coach);
    expect(showCoach({ phase: "exit-badge" }, "Another.", "share")).toEqual({ phase: "exit-badge" });
    expect(showCoach({ phase: "paused", resumeIndex: 1 }, "Another.", "share")).toEqual({
      phase: "paused",
      resumeIndex: 1,
    });
    expect(dismissCoach(card)).toBe(card);
    // The tour's own buttons do nothing to a coach card.
    expect(nextCard(coach, CARDS)).toBe(coach);
    expect(dismissBadge(coach)).toBe(coach);
    expect(dismissPause(coach)).toBe(coach);
  });

  it("is shown while the tour is idle, rings its control, and clears the ring when Got it is pressed", async () => {
    const api = new FakeTourApi({ isSeen: true, isFinished: true });
    const { tour, highlights } = controller(api);
    await tour.load();
    tour.showCoachCard("Your connections live here.", "permissions");
    expect(tour.getState()).toEqual({
      phase: "coach",
      text: "Your connections live here.",
      chromeAnchor: "permissions",
    });
    expect(tour.shownCard()).toBeNull();
    expect(highlights).toEqual(["permissions"]);
    tour.dismissCoach();
    expect(tour.getState()).toEqual(IDLE_TOUR);
    expect(highlights).toEqual(["permissions", null]);
    expect(api.writes).toEqual([]);
  });

  it("is dropped while another coach card, a tour card, the pause notice or the exit badge is up", async () => {
    const api = new FakeTourApi({ isSeen: true, isFinished: true });
    const { tour, highlights } = controller(api);
    await tour.load();
    tour.showCoachCard("First.", "permissions");
    tour.showCoachCard("Second.", "share");
    expect(tour.getState()).toEqual({ phase: "coach", text: "First.", chromeAnchor: "permissions" });
    expect(highlights).toEqual(["permissions"]);
    tour.dismissCoach();

    tour.start();
    tour.showCoachCard("Over a card.", "help");
    expect(tour.shownCard()?.id).toBe("a");
    tour.noteAgentWorking(true);
    tour.noteAgentWorking(false);
    tour.next();
    tour.showCoachCard("Over the pause.", "help");
    expect(tour.getState()).toEqual({ phase: "paused", resumeIndex: 1 });
    tour.dismissPause();

    tour.start();
    tour.exit();
    tour.showCoachCard("Over the badge.", "help");
    expect(tour.getState()).toEqual({ phase: "exit-badge" });
    expect(highlights).toEqual(["permissions", null]);
  });

  it("gives way to a tour that starts, from the launcher or by itself, and its ring goes with it", async () => {
    const api = new FakeTourApi({ isSeen: true, resumeIndex: 1, isFinished: true });
    const { tour, scheduled, highlights } = controller(api, { startAfterWorkingMs: 15000 });
    await tour.load();
    tour.noteAgentWorking(true);
    tour.showCoachCard("Your connections live here.", "permissions");
    expect(highlights).toEqual(["permissions"]);
    // The start the agent's work scheduled comes due under the coach card, and takes its place.
    fire(scheduled);
    expect(tour.shownCard()?.id).toBe("b");
    expect(highlights).toEqual(["permissions", "share"]);
    tour.exit();
    tour.dismissBadge();

    tour.showCoachCard("Your connections live here.", "permissions");
    tour.start();
    expect(tour.shownCard()?.id).toBe("a");
    expect(highlights).toEqual(["permissions", "share", null, "permissions", null]);
  });
});
