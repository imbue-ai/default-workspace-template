// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { chatImportSourceRecord as source } from "../testing/records";
import type { ChatImport, ChatImportSource } from "./ChatImport";
import { cardPhase, dismissChatImport, getChatImport, importedSummary, refreshChatImport } from "./ChatImport";

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

describe("putting the card away", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("is not undone by the answer to a load the server read before it", async () => {
    const answers: Array<(response: Response) => void> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>((resolve) => answers.push(resolve))),
    );
    const loading = refreshChatImport();
    const dismissing = dismissChatImport();
    const [load, dismissal] = answers;

    dismissal(new Response(JSON.stringify(chatImport({}, true)), { status: 200 }));
    await dismissing;
    load(new Response(JSON.stringify(chatImport({})), { status: 200 }));
    await loading;

    expect(getChatImport()?.is_dismissed).toBe(true);
  });
});
