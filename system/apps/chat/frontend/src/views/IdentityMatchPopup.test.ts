// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

// The popup answers through the store's two verbs; they are recorded here, and resolve or fail as a test
// says, while everything else in the store is the real one.
const state = vi.hoisted(() => ({
  confirmed: [] as [string, number][],
  rejected: [] as string[],
  failure: null as Error | null,
}));
vi.mock("../models/IdentityMatches", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../models/IdentityMatches")>();
  return {
    ...actual,
    confirmIdentityMatch: (matchId: string, index: number) => {
      state.confirmed.push([matchId, index]);
      return state.failure === null ? Promise.resolve() : Promise.reject(state.failure);
    },
    rejectIdentityMatch: (matchId: string) => {
      state.rejected.push(matchId);
      return state.failure === null ? Promise.resolve() : Promise.reject(state.failure);
    },
  };
});

import m from "mithril";
import type { IdentityMatch } from "../models/IdentityMatches";
import { applyIdentityMatchUpdate, forgetIdentityMatches, getPendingIdentityMatch } from "../models/IdentityMatches";
import { IdentityMatchPopup } from "./IdentityMatchPopup";

const MATCH: IdentityMatch = {
  match_id: "identity-1",
  chat_id: "agent-1",
  searched_for: "Ada Lovelace, London",
  candidates: [
    {
      name: "Ada Lovelace",
      summary: "Mathematician, London",
      source_url: "https://www.example.org/ada",
      source_title: "Ada Lovelace - Example",
    },
    { name: "Ada King", summary: "Countess", source_url: "https://other.example.com/king", source_title: "King" },
  ],
  status: "pending",
  chosen_index: null,
};

const ROOT = () => document.getElementById("root") as HTMLElement;

function render(chatId = "agent-1"): void {
  m.render(ROOT(), m(IdentityMatchPopup as never, { chatId }));
}

function button(label: string): HTMLButtonElement {
  const found = [...ROOT().querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
  if (found === undefined) throw new Error(`no button "${label}"`);
  return found;
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  document.body.innerHTML = '<div id="root"></div>';
  state.confirmed = [];
  state.rejected = [];
  state.failure = null;
  forgetIdentityMatches();
});

describe("the identity-match popup", () => {
  it("shows nothing while no look-up is waiting for this chat", () => {
    applyIdentityMatchUpdate({ ...MATCH, chat_id: "agent-2" });
    render();
    expect(ROOT().textContent).toBe("");
  });

  it("lists each candidate with what was searched for and a link to its page", () => {
    applyIdentityMatchUpdate(MATCH);
    render();

    const text = ROOT().textContent ?? "";
    expect(text).toContain("Is one of these you?");
    expect(text).toContain('I looked you up on the web as "Ada Lovelace, London".');
    expect(text).toContain("Mathematician, London");
    const links = [...ROOT().querySelectorAll("a")];
    expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual([
      ["example.org", "https://www.example.org/ada"],
      ["other.example.com", "https://other.example.com/king"],
    ]);
    expect(links.every((link) => link.target === "_blank" && link.rel === "noopener noreferrer")).toBe(true);
    expect(ROOT().querySelectorAll(".identity-candidate")).toHaveLength(2);
  });

  it("asks about one person in the singular", () => {
    applyIdentityMatchUpdate({ ...MATCH, candidates: [MATCH.candidates[0]] });
    render();
    expect(ROOT().textContent).toContain("Is this you?");
  });

  it("confirms the candidate whose row was pressed", async () => {
    applyIdentityMatchUpdate(MATCH);
    render();

    const secondRowButton = ROOT().querySelectorAll(".identity-candidate")[1].querySelector("button");
    secondRowButton?.click();
    await settle();

    expect(state.confirmed).toEqual([["identity-1", 1]]);
  });

  it("rejects them all with None of these", async () => {
    applyIdentityMatchUpdate(MATCH);
    render();

    button("None of these").click();
    await settle();

    expect(state.rejected).toEqual(["identity-1"]);
  });

  it("says when an answer did not go through, and keeps the popup up", async () => {
    applyIdentityMatchUpdate(MATCH);
    state.failure = new Error("the chat app is restarting");
    render();

    button("None of these").click();
    await settle();
    render();

    expect(ROOT().querySelector('[role="alert"]')?.textContent).toContain("That didn't go through");
    expect(getPendingIdentityMatch("agent-1")?.match_id).toBe("identity-1");
  });

  it("Escape sets it aside on this page without answering", () => {
    applyIdentityMatchUpdate(MATCH);
    render();

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    render();

    expect(ROOT().textContent).toBe("");
    expect(state.confirmed).toEqual([]);
    expect(state.rejected).toEqual([]);
  });
});
