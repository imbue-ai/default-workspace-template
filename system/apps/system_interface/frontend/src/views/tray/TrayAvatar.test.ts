// @vitest-environment jsdom
import "../../testing/dom";
import { describe, expect, it } from "vitest";
import m from "mithril";
import { rosterParticipantRecord } from "../../testing/records";
import { TrayAvatar } from "./TrayAvatar";

describe("TrayAvatar", () => {
  function draw(root: HTMLElement, pictureUrl: string | null): void {
    const entry = rosterParticipantRecord("user:user-bob-4471", {
      display_name: "Bob",
      is_connected: true,
      profile_picture_url: pictureUrl,
    });
    m.render(root, m(TrayAvatar, { entry, sizePx: 32, isFaded: false }));
  }

  it("treats an empty picture URL as no picture and draws the monogram", () => {
    const root = document.createElement("div");
    draw(root, "");
    expect(root.querySelector("img")).toBeNull();
    expect(root.textContent).toBe("B");
  });

  it("falls back to the monogram when the picture fails to load, and tries a new URL afresh", () => {
    const root = document.createElement("div");
    draw(root, "https://accounts.example.com/gone.png");
    root.querySelector("img")?.dispatchEvent(new Event("error"));
    draw(root, "https://accounts.example.com/gone.png");
    expect(root.querySelector("img")).toBeNull();
    expect(root.textContent).toBe("B");

    draw(root, "https://accounts.example.com/new.png");
    expect(root.querySelector("img")?.getAttribute("src")).toBe("https://accounts.example.com/new.png");
  });
});
