// @vitest-environment jsdom
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import { afterEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { appRecord, clientRecord, desktopRecord, rosterParticipantRecord, windowRecord } from "../../testing/records";
import { PersonCard, lookingAtOf } from "./PersonCard";
import type { PersonCardAttrs } from "./PersonCard";

const NOW = new Date("2026-10-25T12:00:00Z");
const bob = rosterParticipantRecord("user:user-bob-4471", {
  display_name: "Bob",
  is_connected: true,
  last_input_at: "2026-10-25T11:59:50Z",
});
const josh = rosterParticipantRecord("user:user-josh-0001", {
  display_name: "Josh",
  is_owner: true,
  is_connected: true,
});
const files = appRecord("files");
const work = desktopRecord("work", {
  name: "Work",
  windows: [windowRecord("win-1", "files", "/reports", { title: "Reports" }), windowRecord("win-2", "chat", "/")],
});
const home = desktopRecord("home", { name: "Home" });

function render(overrides: Partial<PersonCardAttrs> = {}): HTMLElement {
  const attrs: PersonCardAttrs = {
    anchor: null,
    onEnter: vi.fn(),
    onLeave: vi.fn(),
    entry: bob,
    isOwn: false,
    now: NOW,
    clients: [clientRecord("bob-laptop", { participant: bob.key, active_desktop: "work", shown: ["win-1", "win-2"] })],
    desktops: [home, work],
    apps: [files],
    activeDesktopId: "home",
    onOpenDesktop: vi.fn(),
    onNewDesktop: vi.fn(),
    ...overrides,
  };
  return mountView(() => m(PersonCard, attrs));
}

afterEach(() => {
  unmountViews();
});

describe("lookingAtOf", () => {
  it("reads the top shown window of the newest connected client of theirs, and the owner's clients under either key", () => {
    const older = clientRecord("bob-phone", {
      participant: bob.key,
      active_desktop: "home",
      last_seen: "2026-10-25T10:00:00Z",
      shown: [],
    });
    const newer = clientRecord("bob-laptop", {
      participant: bob.key,
      active_desktop: "work",
      last_seen: "2026-10-25T11:00:00Z",
      shown: ["win-2", "win-1"],
    });
    expect(lookingAtOf(bob, [older, newer], [home, work], [files])?.window.id).toBe("win-2");
    expect(lookingAtOf(bob, [older], [home, work], [files])).toBeNull();
    const unsharedOwner = clientRecord("laptop", { participant: "owner", active_desktop: "work", shown: ["win-1"] });
    expect(lookingAtOf(josh, [unsharedOwner], [home, work], [files])?.window.id).toBe("win-1");
    expect(lookingAtOf(bob, [unsharedOwner], [home, work], [files])).toBeNull();
  });
});

describe("PersonCard", () => {
  it("shows the name, the status, what they are looking at with its app icon, and no desktops while they own none", () => {
    const root = render();
    expect(root.querySelector("[data-tray-card-name]")?.textContent).toBe("Bob");
    expect(root.querySelector("[data-tray-card-status]")?.textContent).toBe("Active");
    expect(root.querySelector("[data-tray-card-looking='win-1']")?.textContent).toContain("Reports");
    expect(root.querySelector("[data-tray-card-looking='win-1']")?.textContent).toContain("Looking at");
    expect(root.querySelector("[data-tray-card-desktops]")).toBeNull();
  });

  it("lists the desktops a person owns, marks the one the viewer is on, and goes to one on a click", () => {
    const onOpenDesktop = vi.fn();
    const bobs = desktopRecord("bob", { name: "Bob's desk", owner: bob.key });
    const root = render({ desktops: [home, work, bobs], activeDesktopId: "bob", onOpenDesktop });
    const rows = Array.from(root.querySelectorAll<HTMLElement>("[data-tray-card-desktop]"));
    expect(rows.map((row) => row.getAttribute("data-tray-card-desktop"))).toEqual(["bob"]);
    expect(root.querySelector("[data-tray-card-desktops]")?.textContent).toContain("Bob's desktops");
    expect(rows[0].textContent).toContain("Viewing");
    rows[0].click();
    expect(onOpenDesktop).toHaveBeenCalledWith("bob");
    expect(root.querySelector("[data-tray-card-new-desktop]")).toBeNull();
  });

  it("lists your own desktops (the keyless ones, for the owner), marks the one you are on, switches on a click, and ends with New desktop", () => {
    const onOpenDesktop = vi.fn();
    const onNewDesktop = vi.fn();
    const bobs = desktopRecord("bob", { owner: bob.key });
    const root = render({
      entry: josh,
      isOwn: true,
      clients: [],
      desktops: [home, bobs, work],
      onOpenDesktop,
      onNewDesktop,
    });
    expect(root.querySelector("[data-tray-card-name]")?.textContent).toBe("Josh (you)");
    const rows = Array.from(root.querySelectorAll<HTMLElement>("[data-tray-card-desktop]"));
    expect(rows.map((row) => row.getAttribute("data-tray-card-desktop"))).toEqual(["home", "work"]);
    expect(rows[0].textContent).toContain("Viewing");
    expect(rows[1].textContent).not.toContain("Viewing");
    rows[1].click();
    expect(onOpenDesktop).toHaveBeenCalledWith("work");
    (root.querySelector("[data-tray-card-new-desktop]") as HTMLElement).click();
    expect(onNewDesktop).toHaveBeenCalledTimes(1);
  });

  it("reads You with a silhouette for the owner of an unshared workspace, and Visiting agent with no desktops for a visiting agent", () => {
    const anonymous = rosterParticipantRecord("owner", { display_name: "Owner", is_owner: true, is_connected: true });
    const anonymousCard = render({ entry: anonymous, isOwn: true, clients: [] });
    expect(anonymousCard.querySelector("[data-tray-card-name]")?.textContent).toBe("You");
    expect(anonymousCard.querySelector("[data-tray-avatar='owner']")).not.toBeNull();
    expect(anonymousCard.querySelector("[data-tray-avatar='owner']")?.textContent).toBe("");
    unmountViews();
    const seer = rosterParticipantRecord("agent:agent-7", {
      kind: "visiting_agent",
      display_name: "Seer",
      is_connected: true,
    });
    const root = render({ entry: seer, clients: [] });
    expect(root.querySelector("[data-tray-card-status]")?.textContent).toBe("Visiting agent · Active");
    expect(root.querySelector("[data-tray-card-desktops]")).toBeNull();
    expect(root.querySelector("[data-tray-card-looking]")).toBeNull();
  });
});
