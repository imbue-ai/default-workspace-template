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
  return document.querySelector("[data-tour-card], [data-tour-badge], [data-tour-paused]");
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
    expect(chromeSideOf("help")).toBe("right");
    expect(chromeSideOf("share")).toBe("left");
  });
});
