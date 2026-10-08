import { describe, expect, it } from "vitest";
import type { ChatImport, ChatImportSource } from "./ChatImport";
import { cardPhase, importedSummary } from "./ChatImport";

function source(state: ChatImportSource["state"], conversations = 0): ChatImportSource {
  return { state, conversations, updated_at: "", detail: "" };
}

function chatImport(sources: Record<string, ChatImportSource>, isDismissed = false): ChatImport {
  return { is_dismissed: isDismissed, sources };
}

describe("the chat import card's phase", () => {
  it("is hidden before the state loads and once the card is put away", () => {
    expect(cardPhase(null)).toBe("hidden");
    expect(cardPhase(chatImport({ claude: source("importing") }, true))).toBe("hidden");
  });

  it("offers the import until anything is recorded", () => {
    expect(cardPhase(chatImport({}))).toBe("offer");
  });

  it("follows a running import ahead of any other outcome", () => {
    expect(cardPhase(chatImport({ claude: source("imported"), chatgpt: source("importing") }))).toBe("importing");
  });

  it("asks for the user when a source needs a sign-in or did not finish", () => {
    expect(cardPhase(chatImport({ claude: source("imported"), chatgpt: source("needs_sign_in") }))).toBe("attention");
    expect(cardPhase(chatImport({ claude: source("failed") }))).toBe("attention");
  });

  it("shows the result once every source imported", () => {
    expect(cardPhase(chatImport({ claude: source("imported"), chatgpt: source("imported") }))).toBe("imported");
  });
});

describe("the imported summary", () => {
  it("names each recorded source in the card's order", () => {
    expect(importedSummary({ chatgpt: source("imported", 40), claude: source("imported", 1312) })).toBe(
      `${(1312).toLocaleString()} from Claude and 40 from ChatGPT`,
    );
    expect(importedSummary({ chatgpt: source("imported", 7) })).toBe("7 from ChatGPT");
    expect(importedSummary({})).toBe("");
  });
});
