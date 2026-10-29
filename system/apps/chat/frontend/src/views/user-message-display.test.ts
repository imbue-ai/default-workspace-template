// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Participant } from "../models/Participants";
import { setParticipantsForTesting } from "../models/Participants";
import { authorOfTurn } from "./user-message-display";

vi.mock("../markdown", () => ({ MarkdownContent: { view: () => null } }));

afterEach(() => {
  setParticipantsForTesting(null, []);
});

function person(key: string, display_name: string, is_owner: boolean): Participant {
  return {
    key,
    kind: "person",
    display_name,
    profile_picture_url: null,
    email: null,
    handle: display_name,
    color: "#000",
    is_owner,
    is_connected: true,
    last_seen: null,
    last_input_at: null,
  };
}

const ROSTER = [person("user:owner-1", "Josh", true), person("user:bob-2", "Bob", false)];

describe("authorOfTurn", () => {
  it("draws the owner's turns as the viewer's own on an owner page that still calls itself owner", () => {
    // The page loaded before the workspace was shared, so its own key is the synthetic one; the owner's later
    // turns are annotated under their account key, which the roster marks as the owner's.
    setParticipantsForTesting("owner", ROSTER);

    expect(authorOfTurn({ author: "user:owner-1", author_name: "Josh" })).toBeNull();
    expect(authorOfTurn({ author: "owner", author_name: undefined })).toBeNull();
    expect(authorOfTurn({ author: undefined, author_name: undefined })).toBeNull();
    expect(authorOfTurn({ author: "user:bob-2", author_name: "Bob" })).toEqual({ key: "user:bob-2", name: "Bob" });
  });

  it("draws both of the owner's keys as the owner's entry on a visitor's page", () => {
    setParticipantsForTesting("user:bob-2", ROSTER);

    expect(authorOfTurn({ author: "owner", author_name: undefined })).toEqual({ key: "user:owner-1", name: "Josh" });
    expect(authorOfTurn({ author: "user:owner-1", author_name: undefined })).toEqual({
      key: "user:owner-1",
      name: "Josh",
    });
    expect(authorOfTurn({ author: "user:bob-2", author_name: "Bob" })).toBeNull();
  });

  it("reads every turn as the viewer's own until the page knows who it is", () => {
    setParticipantsForTesting(null, ROSTER);

    expect(authorOfTurn({ author: "user:bob-2", author_name: "Bob" })).toBeNull();
  });
});
