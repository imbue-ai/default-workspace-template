// @vitest-environment jsdom
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import { afterEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { rosterParticipantRecord } from "../../testing/records";
import { PeopleStrip, entryTitle, isStacked, peopleOrder } from "./PeopleStrip";
import type { PeopleStripAttrs } from "./PeopleStrip";

const NOW = new Date("2026-10-25T12:00:00Z");
const josh = rosterParticipantRecord("user:user-josh-0001", {
  display_name: "Josh",
  is_owner: true,
  is_connected: true,
});
const bob = rosterParticipantRecord("user:user-bob-4471", {
  display_name: "Bob",
  is_connected: false,
  profile_picture_url: "https://accounts.example.com/bob.png",
});
const carol = rosterParticipantRecord("email:carol@example.com", {
  kind: "invited",
  display_name: "carol@example.com",
  email: "carol@example.com",
});
const seer = rosterParticipantRecord("agent:agent-7", {
  kind: "visiting_agent",
  display_name: "Seer",
  is_connected: true,
});
const atlas = rosterParticipantRecord("workspace-agent", {
  kind: "workspace_agent",
  display_name: "Atlas",
  is_connected: true,
});

function render(overrides: Partial<PeopleStripAttrs> = {}): HTMLElement {
  const attrs: PeopleStripAttrs = {
    roster: [josh, bob, carol, seer, atlas],
    viewer: josh.key,
    now: NOW,
    arrived: new Set(),
    onPersonClick: vi.fn(),
    onHoverStart: vi.fn(),
    onHoverEnd: vi.fn(),
    ...overrides,
  };
  return mountView(() => m(PeopleStrip, attrs));
}

function entries(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>("[data-tray-person]"));
}

afterEach(() => {
  unmountViews();
});

describe("peopleOrder and entryTitle", () => {
  it("lists everyone but the workspace agent in roster order with the viewer last, and names you and the invited", () => {
    expect(peopleOrder([josh, bob, carol, seer, atlas], josh.key).map((entry) => entry.key)).toEqual([
      bob.key,
      carol.key,
      seer.key,
      josh.key,
    ]);
    expect(entryTitle(josh, true)).toBe("Josh (you)");
    expect(entryTitle(carol, false)).toBe("carol@example.com (invited)");
    expect(isStacked(3)).toBe(false);
    expect(isStacked(4)).toBe(true);
  });
});

describe("PeopleStrip", () => {
  it("draws a picture, a monogram, a dashed envelope, and a monogram tile, faded when not here, stacked at four", () => {
    const root = render();
    const drawn = entries(root);
    expect(drawn.map((element) => element.getAttribute("data-tray-person"))).toEqual([
      bob.key,
      carol.key,
      seer.key,
      josh.key,
    ]);
    expect(drawn[0].querySelector("img")?.getAttribute("src")).toBe("https://accounts.example.com/bob.png");
    expect(drawn[0].querySelector("[data-tray-avatar]")?.getAttribute("style")).toContain("opacity: 0.35");
    expect(drawn[1].querySelector("[data-tray-avatar='invited']")).not.toBeNull();
    expect(drawn[2].querySelector("[data-tray-avatar='visiting-agent']")?.textContent).toBe("S");
    expect(drawn[3].getAttribute("data-tray-self")).toBe("true");
    expect(drawn[3].textContent).toBe("J");
    expect(root.querySelector("[data-tray-widget='people']")?.getAttribute("data-stacked")).toBe("true");
    expect(drawn[1].classList.contains("tray-stacked")).toBe(true);
    expect(drawn[0].classList.contains("tray-stacked")).toBe(false);
  });

  it("opens the Lounge for a person and a visiting agent, and nothing for yourself or an invited email", () => {
    const onPersonClick = vi.fn();
    const root = render({ onPersonClick, roster: [josh, bob, carol] });
    expect(root.querySelector("[data-tray-widget='people']")?.getAttribute("data-stacked")).toBe("false");
    for (const element of entries(root)) element.click();
    expect(onPersonClick.mock.calls.map(([entry]) => entry.key)).toEqual([bob.key]);
  });

  it("hops an entry that just arrived, and tells the caller where the pointer is", () => {
    const onHoverStart = vi.fn();
    const onHoverEnd = vi.fn();
    const root = render({ arrived: new Set([bob.key]), onHoverStart, onHoverEnd });
    const [first] = entries(root);
    expect(first.getAttribute("data-tray-arrived")).toBe("true");
    expect(first.classList.contains("tray-hop")).toBe(true);
    first.dispatchEvent(new MouseEvent("mouseenter"));
    first.dispatchEvent(new MouseEvent("mouseleave"));
    expect(onHoverStart).toHaveBeenCalledWith(bob, first);
    expect(onHoverEnd).toHaveBeenCalledTimes(1);
  });

  it("draws nothing with nobody but the workspace agent in the roster", () => {
    expect(render({ roster: [atlas] }).querySelector("[data-tray-widget='people']")).toBeNull();
  });
});
