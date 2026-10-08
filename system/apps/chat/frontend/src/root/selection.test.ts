import { describe, expect, it } from "vitest";
import { intakeTokenFromSearch, rootPathFor, selectionFromSearch, slotFill } from "./selection";
import type { SlotState } from "./selection";

describe("selection", () => {
  it("reads the selected chat off the query and refuses an id that cannot be a chat's", () => {
    expect(selectionFromSearch("?chat=agent-0123abc")).toBe("agent-0123abc");
    expect(selectionFromSearch("?chat=not%20an%20id")).toBeNull();
    expect(selectionFromSearch("")).toBeNull();
  });

  it("reads the pending intake's token beside the selection, null for none", () => {
    expect(intakeTokenFromSearch("?chat=agent-1&intake=tok-1")).toBe("tok-1");
    expect(intakeTokenFromSearch("?intake=tok-2")).toBe("tok-2");
    expect(intakeTokenFromSearch("?chat=agent-1")).toBeNull();
    expect(intakeTokenFromSearch("?intake=")).toBeNull();
    expect(intakeTokenFromSearch("")).toBeNull();
  });

  it("writes the selection back as the root's path, with no token", () => {
    expect(rootPathFor("agent-0123abc")).toBe("/?chat=agent-0123abc");
    expect(rootPathFor(null)).toBe("/");
  });
});

describe("slotFill", () => {
  const empty: SlotState = {
    selectedChatId: null,
    chatIds: [],
    isChatListKnown: true,
    isChoosing: false,
    isCompact: false,
    isShown: true,
  };

  it("shows the head of the list (the default chat, else the most recent) when nothing is selected", () => {
    expect(slotFill({ ...empty, chatIds: ["agent-welcome", "agent-new", "agent-old"] })).toEqual({
      kind: "select",
      chatId: "agent-welcome",
    });
  });

  it("opens a new chat when there are none, but only while the list is on screen", () => {
    expect(slotFill(empty)).toEqual({ kind: "open_new" });
    expect(slotFill({ ...empty, isShown: false })).toEqual({ kind: "keep" });
  });

  it("keeps what is shown: a selection, a list not read yet, a choice in progress, and the phone list", () => {
    expect(slotFill({ ...empty, selectedChatId: "agent-a", chatIds: ["agent-b"] })).toEqual({ kind: "keep" });
    expect(slotFill({ ...empty, isChatListKnown: false })).toEqual({ kind: "keep" });
    expect(slotFill({ ...empty, isChoosing: true, chatIds: ["agent-b"] })).toEqual({ kind: "keep" });
    expect(slotFill({ ...empty, isCompact: true, chatIds: ["agent-b"] })).toEqual({ kind: "keep" });
  });
});
