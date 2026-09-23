import { describe, expect, it } from "vitest";
import {
  draftFromSearch,
  isNewChatPath,
  isSendPath,
  newChatParamsFromSearch,
  rootPathFor,
  selectionFromSearch,
  sendTextFromSearch,
} from "./selection";

describe("selection", () => {
  it("reads the selected chat off the query and refuses an id that cannot be a chat's", () => {
    expect(selectionFromSearch("?chat=agent-0123abc")).toBe("agent-0123abc");
    expect(selectionFromSearch("?chat=not%20an%20id")).toBeNull();
    expect(selectionFromSearch("")).toBeNull();
  });

  it("reads the draft the root's URL hands the composer, empty for none", () => {
    expect(draftFromSearch("?chat=agent-1&draft=Draw+me+a+seal")).toBe("Draw me a seal");
    expect(draftFromSearch("?chat=agent-1")).toBe("");
    expect(draftFromSearch("")).toBe("");
  });

  it("writes the selection back as the root's path", () => {
    expect(rootPathFor("agent-0123abc")).toBe("/?chat=agent-0123abc");
    expect(rootPathFor(null)).toBe("/");
  });

  it("recognizes the launch path under a base path and reads its params", () => {
    expect(isNewChatPath("/new", "")).toBe(true);
    expect(isNewChatPath("/prefix/new/", "/prefix")).toBe(true);
    expect(isNewChatPath("/", "")).toBe(false);
    expect(newChatParamsFromSearch("?message=hello%20there&account_id=acct-1")).toEqual({
      accountId: "acct-1",
      message: "hello there",
    });
    expect(newChatParamsFromSearch("")).toEqual({ accountId: "", message: "" });
  });

  it("recognizes the send launch path under a base path and reads its text", () => {
    expect(isSendPath("/send", "")).toBe(true);
    expect(isSendPath("/prefix/send/", "/prefix")).toBe(true);
    expect(isSendPath("/new", "")).toBe(false);
    expect(sendTextFromSearch("?message=hello%20there")).toBe("hello there");
    expect(sendTextFromSearch("")).toBe("");
  });
});
