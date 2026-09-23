// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import { afterEach, describe, expect, it } from "vitest";

import m from "mithril";

import { applyPresence, resetPresenceForTesting, setOwnIdentityForTesting } from "../model/Presence";
import { presentUserRecord } from "../testing/records";
import { PresenceStrip, orderedForStrip, presenceInitial, presenceTitle } from "./PresenceStrip";

const bob = presentUserRecord("user-bob-4471", { email: "bob@example.com", display_name: "Bob" });
const owner = presentUserRecord("user-owner-9c21", {
  email: "owner@example.com",
  profile_picture_url: "https://accounts.example.com/users/user-owner-9c21/profile-picture/9a7b",
  owner: true,
});
const carol = presentUserRecord("user-carol-1d2e", { email: "carol@example.com" });

function render(): HTMLElement {
  return mountView(() => m(PresenceStrip));
}

function pictures(root: HTMLElement): Element[] {
  return Array.from(root.querySelectorAll("[data-presence-user]"));
}

/** Make this page ``userId``'s, as the heartbeat's answer would. */
function signInAs(userId: string): void {
  setOwnIdentityForTesting({ owner: false, user_id: userId, email: `${userId}@example.com` });
}

afterEach(() => {
  unmountViews();
  resetPresenceForTesting();
});

describe("presenceInitial and presenceTitle", () => {
  it("prefers the name and falls back to the email, and never shows a name without its email", () => {
    expect(presenceInitial(bob)).toBe("B");
    expect(presenceInitial(owner)).toBe("O");
    expect(presenceTitle(bob)).toBe("Bob (bob@example.com)");
    expect(presenceTitle(owner)).toBe("owner@example.com - owner");
  });
});

describe("orderedForStrip", () => {
  it("keeps everyone else in the shell's order and puts the viewer last", () => {
    expect(orderedForStrip([bob, owner, carol], "user-owner-9c21").map((user) => user.user_id)).toEqual([
      "user-bob-4471",
      "user-carol-1d2e",
      "user-owner-9c21",
    ]);
    expect(orderedForStrip([bob, owner], null)).toEqual([bob, owner]);
    expect(orderedForStrip([bob, owner], "user-nobody")).toEqual([bob, owner]);
  });
});

describe("PresenceStrip", () => {
  it("renders nothing while nobody is recorded, and nothing while only one user is connected", () => {
    expect(render().querySelector(".presence-strip")).toBeNull();
    unmountViews();
    applyPresence([owner]);
    expect(render().querySelector(".presence-strip")).toBeNull();
  });

  it("draws one picture per user once two are connected: an image where there is one and an initial otherwise", () => {
    applyPresence([bob, owner]);
    const root = render();
    const drawn = pictures(root);
    expect(drawn.map((element) => element.getAttribute("data-presence-user"))).toEqual([
      "user-bob-4471",
      "user-owner-9c21",
    ]);
    expect(drawn[0].textContent).toBe("B");
    expect(drawn[1].querySelector("img")?.getAttribute("src")).toBe(owner.profile_picture_url);
    // No identity of our own yet: nobody is marked as you.
    expect(root.querySelector("[data-presence-self]")).toBeNull();
    expect(drawn.map((element) => element.getAttribute("title"))).toEqual([
      "Bob (bob@example.com)",
      "owner@example.com - owner",
    ]);
  });

  it("puts the viewer's own entry last, ringed, and says so on hover", () => {
    signInAs("user-bob-4471");
    applyPresence([bob, owner, carol]);
    const root = render();
    const drawn = pictures(root);
    expect(drawn.map((element) => element.getAttribute("data-presence-user"))).toEqual([
      "user-owner-9c21",
      "user-carol-1d2e",
      "user-bob-4471",
    ]);
    const own = root.querySelector("[data-presence-self='true']") as HTMLElement;
    expect(own.getAttribute("data-presence-user")).toBe("user-bob-4471");
    expect(own.getAttribute("title")).toBe("Bob (bob@example.com) (you)");
    expect(own.className).toContain("ring-accent");
    expect(drawn[0].className).not.toContain("ring-accent");
    expect(drawn[0].getAttribute("title")).toBe("owner@example.com - owner");
  });
});
