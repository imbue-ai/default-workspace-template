// @vitest-environment jsdom
import "../testing/dom";
import m from "mithril";
import { describe, expect, it } from "vitest";
import { appRecord, avatarStateRecord, windowRecord } from "../testing/records";
import type { TaskbarEntry } from "../reducers/desktopState";
import { AvatarImage, entryStyleParts } from "./AvatarImage";
import type { AvatarImageAttrs } from "./AvatarImage";
import type { ImbueCharacterAttrs } from "./character/ImbueCharacter";

const chat = appRecord("chat", {
  pin: { path: "/", style: "avatar", scope: "independent", default_mode: "floating" },
});

/** The pinned chat entry in the avatar style, with the user at it. */
function avatarEntry(overrides: Partial<TaskbarEntry> = {}): TaskbarEntry {
  return {
    window: windowRecord("win-1", "chat", "/", { is_pinned: true }),
    app: chat,
    title: "Chat",
    isMinimized: false,
    isDetached: false,
    isFocused: true,
    isPinned: true,
    look: { mode: "floating", style: "avatar", declaredStyle: "avatar", position: { x: 0.5, y: 0.5 } },
    ...overrides,
  };
}

/** What the entry hands the avatar for "the user is here". */
function attendingOf(entry: TaskbarEntry): boolean | undefined {
  const parts = entryStyleParts(entry, avatarStateRecord(), 32, "size-full");
  const image = parts.image as m.Vnode<{ isAttending: boolean }>;
  return image.attrs.isAttending;
}

describe("whether the user is at the avatar's window", () => {
  it("is true only when the window is focused and on screen", () => {
    expect(attendingOf(avatarEntry())).toBe(true);
  });

  it("is false when the window is not focused", () => {
    expect(attendingOf(avatarEntry({ isFocused: false }))).toBe(false);
  });

  it("is false when the window is minimized, even if it reads as focused", () => {
    // The shell can hold focus on a window it is not drawing, so minimized has to be asked separately.
    expect(attendingOf(avatarEntry({ isMinimized: true }))).toBe(false);
  });

  it("is false when the window is pulled out into its own desktop window", () => {
    expect(attendingOf(avatarEntry({ isDetached: true }))).toBe(false);
  });
});

describe("the character's attributes", () => {
  it("carry the mood and the class the entry chose", () => {
    const parts = entryStyleParts(avatarEntry(), avatarStateRecord({ design: "imbue-character" }), 32, "size-7");
    const attrs = (parts.image as m.Vnode<ImbueCharacterAttrs>).attrs;
    expect(attrs.mood).toEqual("idle");
    expect(attrs.class).toEqual("size-7");
  });

  it("read a caller that leaves the attending state out as the user being elsewhere", () => {
    // A caller drawing the avatar as an identifying icon has no window to answer for, so it omits
    // the attribute -- and the character has to land somewhere, which is slouched.
    const drawn: AvatarImageAttrs = {
      design: "imbue-character",
      defaultDesign: "gummy-seal",
      mood: "idle",
      class: "size-7",
    };
    const character = AvatarImage.view({ attrs: drawn } as m.Vnode<AvatarImageAttrs>) as m.Vnode<ImbueCharacterAttrs>;
    expect(character.attrs.isAttending).toBe(false);
  });
});
