// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { chatImportRecord as chatImport, chatImportSourceRecord as source } from "../testing/records";
import { cardPhase, dismissChatImport, getChatImport, importedSummary, refreshChatImport } from "./ChatImport";

describe("the chat import card's phase", () => {
  it("is hidden before the state loads and once the card is put away", () => {
    expect(cardPhase(null)).toBe("hidden");
    expect(cardPhase(chatImport({ claude: source("importing") }, { is_dismissed: true }))).toBe("hidden");
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

describe("following a running import", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("polls while a source is importing and stops once none is", async () => {
    vi.useFakeTimers();
    vi.resetModules();
    const answers = [chatImport({ claude: source("importing", 3) }), chatImport({ claude: source("imported", 9) })];
    const fetched = vi.fn(async () => new Response(JSON.stringify(answers.shift()), { status: 200 }));
    vi.stubGlobal("fetch", fetched);
    const model = await import("./ChatImport");

    await model.refreshChatImport();
    expect(fetched).toHaveBeenCalledTimes(1);
    expect(model.cardPhase(model.getChatImport())).toBe("importing");

    await vi.advanceTimersByTimeAsync(model.POLL_INTERVAL_MS);
    expect(fetched).toHaveBeenCalledTimes(2);
    expect(model.cardPhase(model.getChatImport())).toBe("imported");

    await vi.advanceTimersByTimeAsync(3 * model.POLL_INTERVAL_MS);
    expect(fetched).toHaveBeenCalledTimes(2);
  });

  it("looks for the import a started chat brings, until it has come and gone", async () => {
    vi.useFakeTimers();
    vi.resetModules();
    const answers = [
      chatImport({}),
      chatImport({}),
      chatImport({ claude: source("importing", 3) }),
      chatImport({ claude: source("imported", 9) }),
    ];
    const fetched = vi.fn(async () => new Response(JSON.stringify(answers.shift()), { status: 200 }));
    vi.stubGlobal("fetch", fetched);
    const model = await import("./ChatImport");
    await model.refreshChatImport();

    await vi.advanceTimersByTimeAsync(3 * model.POLL_INTERVAL_MS);
    expect(fetched).toHaveBeenCalledTimes(1);

    model.watchForImportStart();
    await vi.advanceTimersByTimeAsync(2 * model.POLL_INTERVAL_MS);
    expect(model.cardPhase(model.getChatImport())).toBe("importing");

    await vi.advanceTimersByTimeAsync(model.POLL_INTERVAL_MS);
    expect(model.cardPhase(model.getChatImport())).toBe("imported");

    await vi.advanceTimersByTimeAsync(3 * model.POLL_INTERVAL_MS);
    expect(fetched).toHaveBeenCalledTimes(4);
  });

  it("stops looking when no import has come within the watch", async () => {
    vi.useFakeTimers();
    vi.resetModules();
    const fetched = vi.fn(async () => new Response(JSON.stringify(chatImport({})), { status: 200 }));
    vi.stubGlobal("fetch", fetched);
    const model = await import("./ChatImport");
    await model.refreshChatImport();

    model.watchForImportStart();
    await vi.advanceTimersByTimeAsync(model.IMPORT_START_WATCH_MS + model.POLL_INTERVAL_MS);
    const fetchedDuringTheWatch = fetched.mock.calls.length;
    expect(fetchedDuringTheWatch).toBeGreaterThan(1);

    await vi.advanceTimersByTimeAsync(3 * model.POLL_INTERVAL_MS);
    expect(fetched).toHaveBeenCalledTimes(fetchedDuringTheWatch);
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

    dismissal(new Response(JSON.stringify(chatImport({}, { is_dismissed: true })), { status: 200 }));
    await dismissing;
    load(new Response(JSON.stringify(chatImport({})), { status: 200 }));
    await loading;

    expect(getChatImport()?.is_dismissed).toBe(true);
  });
});
