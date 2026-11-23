// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import { TourController } from "../model/tour";
import { TOUR_EXIT_BADGE_TEXT, TOUR_PAUSED_TEXT } from "../model/tourCards";
import { FAKE_TOUR_CARDS as CARDS, FakeTourApi } from "../testing/fakeTour";
import { TourCard, chromeSideOf, placeCard } from "./TourCard";

afterEach(unmountViews);

function controller(): { tour: TourController; highlights: (string | null)[] } {
  const highlights: (string | null)[] = [];
  const tour = new TourController({
    api: new FakeTourApi({ isSeen: true }),
    highlightChrome: (anchor) => highlights.push(anchor),
    isAutoStartAllowed: () => true,
    isViewerOwner: () => true,
    redraw: () => m.redraw.sync(),
    cards: CARDS,
  });
  return { tour, highlights };
}

/** Mount the card over the tour, which draws nothing once the tour is idle; `openLink` records where a link went. */
function mountCard(tour: TourController, openLink: (url: string) => void = () => undefined): void {
  mountView(() =>
    tour.getState().phase === "idle"
      ? null
      : m(TourCard, { tour, avatarDesign: "jelly-cat", openLink, viewport: { width: 1200, height: 800 } }),
  );
}

function card(): HTMLElement | null {
  return document.querySelector("[data-tour-card], [data-tour-badge], [data-tour-paused], [data-tour-coach]");
}

function press(marker: string): void {
  const button = document.querySelector<HTMLElement>(`[${marker}]`);
  expect(button, marker).not.toBeNull();
  button?.click();
  m.redraw.sync();
}

describe("TourCard", () => {
  it("shows each card's words, its link and picture when it has them, and the footer's verbs", () => {
    const { tour } = controller();
    tour.start();
    const opened: string[] = [];
    mountCard(tour, (url) => opened.push(url));
    expect(card()?.getAttribute("data-tour-card")).toBe("a");
    expect(card()?.textContent).toContain("Hello there.");
    expect(document.querySelector("[data-tour-back]")).toBeNull();
    expect(document.querySelector("[data-tour-next]")?.textContent).toBe("Next");

    press("data-tour-next");
    expect(card()?.getAttribute("data-tour-card")).toBe("b");
    const link = document.querySelector<HTMLElement>("[data-tour-link]");
    expect(link?.getAttribute("href")).toBe("https://example.com/discord");
    // The link sits in a row of its own, at the card's right edge.
    expect(link?.parentElement?.classList.contains("justify-end")).toBe(true);
    // A plain click goes through the shell's link routing; a modified click is left to the browser.
    link?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
    link?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0, metaKey: true }));
    expect(opened).toEqual(["https://example.com/discord"]);
    expect(document.querySelector("[data-tour-back]")).not.toBeNull();

    press("data-tour-next");
    expect(card()?.getAttribute("data-tour-card")).toBe("c");
    expect(document.querySelector("[data-tour-card] img[alt='devices']")).not.toBeNull();
    expect([...document.querySelectorAll("[data-tour-card] li")].map((item) => item.textContent)).toEqual([
      "Laptops",
      "Phones",
    ]);
    expect(document.querySelector("[data-tour-next]")?.textContent).toBe("Done");
    press("data-tour-back");
    expect(card()?.getAttribute("data-tour-card")).toBe("b");
  });

  it("leaves the exit badge on Exit walkthrough, which Got it dismisses", () => {
    const { tour, highlights } = controller();
    tour.start();
    tour.next();
    mountCard(tour);
    press("data-tour-exit");
    expect(card()?.hasAttribute("data-tour-badge")).toBe(true);
    expect(card()?.textContent).toContain(TOUR_EXIT_BADGE_TEXT);
    expect(highlights).toEqual(["share", null]);
    press("data-tour-next");
    expect(card()).toBeNull();
  });

  it("shows the pause notice after the agent's reply interrupted a card, which OK dismisses", () => {
    const { tour } = controller();
    tour.start();
    tour.noteAgentWorking(true);
    tour.noteAgentWorking(false);
    mountCard(tour);
    expect(card()?.getAttribute("data-tour-card")).toBe("a");
    press("data-tour-next");
    expect(card()?.hasAttribute("data-tour-paused")).toBe(true);
    for (const paragraph of TOUR_PAUSED_TEXT.split("\n\n")) expect(card()?.textContent).toContain(paragraph);
    expect(card()?.querySelectorAll("p").length).toBe(2);
    expect(document.querySelector("[data-tour-exit]")).toBeNull();
    press("data-tour-next");
    expect(card()).toBeNull();
  });

  it("draws the words a card's text puts between asterisk pairs in bold, and the rest plain", () => {
    const { tour } = controller();
    tour.showCoachCard("**Just ask!** Any time.\n\nSee your **settings** as well.", { chromeAnchor: "permissions" });
    mountCard(tour);
    const paragraphs = [...(card()?.querySelectorAll("p") ?? [])];
    expect(paragraphs.map((paragraph) => paragraph.textContent)).toEqual([
      "Just ask! Any time.",
      "See your settings as well.",
    ]);
    expect(
      paragraphs.map((paragraph) => [...paragraph.querySelectorAll("strong")].map((run) => run.textContent)),
    ).toEqual([["Just ask!"], ["settings"]]);
  });

  it("shows a page's coach card as its words and one button, which Got it dismisses and clears the ring", () => {
    const { tour, highlights } = controller();
    tour.showCoachCard("Your connections live here.\n\nAsk me to connect any of them.", {
      chromeAnchor: "permissions",
    });
    mountCard(tour);
    expect(card()?.getAttribute("data-tour-coach")).toBe("permissions");
    expect(card()?.hasAttribute("data-tour-card")).toBe(false);
    expect([...(card()?.querySelectorAll("p") ?? [])].map((paragraph) => paragraph.textContent)).toEqual([
      "Your connections live here.",
      "Ask me to connect any of them.",
    ]);
    // One button, and none of a tour card's verbs.
    expect(card()?.querySelectorAll("button")).toHaveLength(1);
    expect(document.querySelector("[data-tour-next]")?.textContent).toBe("Got it");
    expect(document.querySelector("[data-tour-exit]")).toBeNull();
    expect(document.querySelector("[data-tour-back]")).toBeNull();
    // Under the title bar, on the side its control is on, arrow up.
    expect(card()?.style.top).toBe("12px");
    expect(card()?.style.left).toBe("160px");
    expect(card()?.querySelector(".tour-arrow")).not.toBeNull();
    expect(highlights).toEqual(["permissions"]);

    press("data-tour-next");
    expect(card()).toBeNull();
    expect(highlights).toEqual(["permissions", null]);
  });

  it("shows a coach card at the avatar with its one button, no arrow, and no control of the chrome ringed", () => {
    const { tour, highlights } = controller();
    tour.showCoachCard("Tell me what to build.", { target: "avatar" });
    mountCard(tour);
    expect(card()?.getAttribute("data-tour-coach")).toBe("avatar");
    expect(card()?.textContent).toContain("Tell me what to build.");
    expect(card()?.querySelectorAll("button")).toHaveLength(1);
    expect(document.querySelector("[data-tour-next]")?.textContent).toBe("Got it");
    expect(card()?.querySelector(".tour-arrow")).toBeNull();
    expect(highlights).toEqual([]);

    press("data-tour-next");
    expect(card()).toBeNull();
  });

  it("shows a coach card asked for over the tour's last card in that card's place", () => {
    const { tour } = controller();
    tour.start();
    tour.next();
    tour.next();
    mountCard(tour);
    expect(card()?.getAttribute("data-tour-card")).toBe("c");

    tour.showCoachCard("Your connections live here.", { chromeAnchor: "permissions" });

    expect(card()?.getAttribute("data-tour-coach")).toBe("permissions");
    expect(card()?.hasAttribute("data-tour-card")).toBe(false);
    expect(document.querySelector("[data-tour-exit]")).toBeNull();
  });
});

describe("placeCard", () => {
  it("sits beside the shell's marker a card names, under the chrome for a chrome card, and by the avatar otherwise", () => {
    const root = document.createElement("div");
    const launcher = document.createElement("div");
    launcher.setAttribute("data-launcher-field", "");
    launcher.getBoundingClientRect = () => ({ left: 100, top: 760, width: 300, height: 32 }) as DOMRect;
    const avatar = document.createElement("button");
    avatar.setAttribute("data-pinned-entry", "chat");
    avatar.getBoundingClientRect = () => ({ left: 24, top: 680, width: 72, height: 72 }) as DOMRect;
    root.append(launcher, avatar);
    const viewport = { width: 1200, height: 800 };
    const size = { width: 360, height: 120 };

    expect(placeCard(CARDS[2], size, root, viewport).arrow?.side).toBe("bottom");
    expect(placeCard(CARDS[1], size, root, viewport)).toEqual({
      left: 160,
      top: 12,
      arrow: { side: "top", offset: 40 },
    });
    expect(placeCard(CARDS[0], size, root, viewport)).toEqual({ left: 24 + 72 + 12, top: 680 - 120, arrow: null });
    // The badge points at the launcher; the pause notice pops out of the avatar.
    expect(placeCard("badge", size, root, viewport).arrow?.side).toBe("bottom");
    expect(placeCard("paused", size, root, viewport)).toEqual({ left: 24 + 72 + 12, top: 680 - 120, arrow: null });
    // A coach card sits under the side of the title bar its control is on, wherever the shell's own markers are.
    expect(placeCard({ coachPlace: { chromeAnchor: "permissions" } }, size, root, viewport)).toEqual({
      left: 160,
      top: 12,
      arrow: { side: "top", offset: 40 },
    });
    expect(placeCard({ coachPlace: { chromeAnchor: "help" } }, size, root, viewport)).toEqual({
      left: 1200 - 360 - 12,
      top: 12,
      arrow: { side: "top", offset: 320 },
    });
    // A coach card at the avatar pops out of it as the tour's own avatar cards do, with no arrow.
    expect(placeCard({ coachPlace: { target: "avatar" } }, size, root, viewport)).toEqual(
      placeCard(CARDS[0], size, root, viewport),
    );
    expect(chromeSideOf("help")).toBe("right");
    expect(chromeSideOf("share")).toBe("left");
  });
});
