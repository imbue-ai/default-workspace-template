// @vitest-environment jsdom
import "../testing/dom";
import m from "mithril";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import { afterEach, describe, expect, it, vi } from "vitest";
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
    const vnode = { attrs: drawn } as m.Vnode<AvatarImageAttrs>;
    const character = AvatarImage(vnode).view(vnode) as m.Vnode<ImbueCharacterAttrs>;
    expect(character.attrs.isAttending).toBe(false);
  });
});

describe("whether the character has just been chosen", () => {
  /** One avatar, drawn through `designs` in turn; what it hands the character each time (null: an image). */
  function arrivals(designs: readonly string[]): Array<boolean | null> {
    const attrsFor = (design: string): AvatarImageAttrs => ({
      design,
      defaultDesign: "gummy-seal",
      mood: "idle",
      class: "size-7",
    });
    const first = { attrs: attrsFor(designs[0]) } as m.Vnode<AvatarImageAttrs>;
    const avatar = AvatarImage(first);
    return designs.map((design) => {
      const drawn = avatar.view({ attrs: attrsFor(design) } as m.Vnode<AvatarImageAttrs>) as m.Vnode<
        Partial<ImbueCharacterAttrs>
      >;
      return drawn.tag === "img" ? null : drawn.attrs.isArriving === true;
    });
  }

  it("is true when the design switches to it", () => {
    expect(arrivals(["gummy-seal", "imbue-character"])).toEqual([null, true]);
  });

  it("is false when it is the first thing drawn, as on a page load or an entry drawn somewhere new", () => {
    expect(arrivals(["imbue-character"])).toEqual([false]);
  });

  it("is false on a redraw that keeps it, so only the switch counts", () => {
    expect(arrivals(["gummy-seal", "imbue-character", "imbue-character"])).toEqual([null, true, false]);
  });

  it("is true again each time it is chosen anew", () => {
    expect(arrivals(["imbue-character", "jelly-cat", "imbue-character"])).toEqual([false, null, true]);
  });
});

describe("switching to the character on a live page", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    unmountViews();
  });

  /** The highest the character's body rises over `count` frames, in viewBox units. */
  async function highestOver(root: HTMLElement, count: number): Promise<number> {
    let highest = 0;
    for (let i = 0; i < count; i++) {
      await vi.advanceTimersByTimeAsync(16);
      const transform = root.querySelector("[data-character-body]")?.getAttribute("transform") ?? "";
      highest = Math.max(highest, -Number(/translate\(-?[\d.]+ (-?[\d.]+)\)/.exec(transform)?.[1] ?? 0));
    }
    return highest;
  }

  async function mountWith(design: () => string): Promise<HTMLElement> {
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
    );
    vi.useFakeTimers();
    return mountView(() =>
      m(AvatarImage, { design: design(), defaultDesign: "gummy-seal", mood: "idle", class: "size-7" }),
    );
  }

  it("makes it jump as it replaces the picture", async () => {
    let design = "gummy-seal";
    const root = await mountWith(() => design);
    expect(root.querySelector("img")).not.toBeNull();
    design = "imbue-character";
    m.redraw.sync();
    expect(await highestOver(root, 45)).toBeGreaterThan(40);
  });

  it("leaves it on the floor when the page opens on it", async () => {
    const root = await mountWith(() => "imbue-character");
    expect(await highestOver(root, 45)).toBeLessThan(5);
  });
});
