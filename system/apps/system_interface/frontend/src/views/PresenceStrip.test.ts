// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import { afterEach, describe, expect, it } from "vitest";

import m from "mithril";

import { applyPresence, resetPresenceForTesting } from "../model/Presence";
import { presentUserRecord } from "../testing/records";
import { PresenceStrip, presenceInitial, presenceTitle } from "./PresenceStrip";

const bob = presentUserRecord("user-bob-4471", { email: "bob@example.com", display_name: "Bob", session_count: 2 });
const owner = presentUserRecord("user-owner-9c21", {
  email: "owner@example.com",
  avatar_url: "https://accounts.example.com/users/user-owner-9c21/avatar/9a7b",
  owner: true,
  session_count: 2,
});

function render(): HTMLElement {
  return mountView(() => m(PresenceStrip));
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

describe("PresenceStrip", () => {
  it("renders nothing while nobody is recorded", () => {
    expect(render().querySelector(".presence-strip")).toBeNull();
  });

  it("draws one avatar per user, an image where there is one and an initial otherwise", () => {
    applyPresence([bob, owner]);
    const root = render();
    const avatars = Array.from(root.querySelectorAll("[data-presence-user]"));
    expect(avatars.map((element) => element.getAttribute("data-presence-user"))).toEqual([
      "user-bob-4471",
      "user-owner-9c21",
    ]);
    expect(avatars[0].textContent).toBe("B");
    expect(avatars[1].querySelector("img")?.getAttribute("src")).toBe(owner.avatar_url);
    // No identity of our own yet, so no refresh link.
    expect(root.querySelector(".presence-refresh")).toBeNull();
  });
});
