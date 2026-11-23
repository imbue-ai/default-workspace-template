// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import {
  isComposerFragmentInDraft,
  readComposerDraft,
  takeComposerDraftChange,
  toggleComposerFragment,
  withComposerFragmentToggled,
} from "./ComposerDraft";

const CHAT = "agent-first-run";

describe("withComposerFragmentToggled", () => {
  it("adds a label and a comma to an empty draft", () => {
    expect(withComposerFragmentToggled("", "Sales")).toBe("Sales, ");
  });

  it("adds each further label at the end, so they stand in the order they were added", () => {
    expect(withComposerFragmentToggled("Sales, ", "Money")).toBe("Sales, Money, ");
  });

  it("takes a label out again, wherever it stands, and leaves the rest", () => {
    expect(withComposerFragmentToggled("Sales, Money, Health, ", "Money")).toBe("Sales, Health, ");
    expect(withComposerFragmentToggled("Sales, ", "Sales")).toBe("");
  });

  it("sets a label off from words the user typed", () => {
    expect(withComposerFragmentToggled("dog training", "Money")).toBe("dog training, Money, ");
    expect(withComposerFragmentToggled("dog training,", "Money")).toBe("dog training, Money, ");
    expect(withComposerFragmentToggled("dog training\n", "Money")).toBe("dog training\nMoney, ");
  });

  it("tells a label from a longer one that ends the same way", () => {
    expect(withComposerFragmentToggled("Google Drive, ", "Drive")).toBe("Google Drive, Drive, ");
    expect(withComposerFragmentToggled("Google Drive, Drive, ", "Drive")).toBe("Google Drive, ");
  });

  it("finds a label after a line break as well as after a comma", () => {
    expect(withComposerFragmentToggled("my own thing\nMoney, ", "Money")).toBe("my own thing\n");
  });
});

describe("toggleComposerFragment", () => {
  beforeEach(() => {
    localStorage.clear();
    takeComposerDraftChange(CHAT);
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
  });

  it("writes the toggled draft where the composer saves its own, and says the draft changed once", () => {
    toggleComposerFragment(CHAT, "Sales");
    toggleComposerFragment(CHAT, "Money");

    expect(localStorage.getItem(`message-text:${CHAT}`)).toBe("Sales, Money, ");
    expect(readComposerDraft(CHAT)).toBe("Sales, Money, ");
    expect(isComposerFragmentInDraft(CHAT, "Money")).toBe(true);
    expect(isComposerFragmentInDraft(CHAT, "Health")).toBe(false);
    expect(takeComposerDraftChange(CHAT)).toBe(true);
    expect(takeComposerDraftChange(CHAT)).toBe(false);
    expect(m.redraw).toHaveBeenCalledTimes(2);
  });

  it("leaves no saved draft behind when the last label is taken out", () => {
    toggleComposerFragment(CHAT, "Sales");
    toggleComposerFragment(CHAT, "Sales");

    expect(localStorage.getItem(`message-text:${CHAT}`)).toBeNull();
    expect(isComposerFragmentInDraft(CHAT, "Sales")).toBe(false);
  });

  it("changes one chat's draft only", () => {
    toggleComposerFragment(CHAT, "Sales");

    expect(readComposerDraft("agent-other")).toBe("");
    expect(takeComposerDraftChange("agent-other")).toBe(false);
  });
});
