// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatImport } from "../models/ChatImport";
import { chatImportRecord, chatImportSourceRecord as source } from "../testing/records";
import { ChatImportCard, IMPORT_PROMPT } from "./ChatImportCard";

afterEach(() => {
  unmountViews();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function render(chatImport: ChatImport | null): { root: HTMLElement; started: string[]; dismissals: number[] } {
  const started: string[] = [];
  const dismissals: number[] = [];
  const root = mountView(() =>
    m(ChatImportCard, {
      chatImport,
      onStartWithText: (text) => started.push(text),
      onDismiss: () => dismissals.push(1),
    }),
  );
  return { root, started, dismissals };
}

function click(root: HTMLElement, action: string): void {
  root.querySelector<HTMLElement>(`[data-chat-import-action="${action}"]`)!.click();
}

describe("the chat import card", () => {
  it("renders nothing before its state loads or once put away", () => {
    expect(render(null).root.querySelector('[data-section="chat-import"]')).toBeNull();
    expect(
      render(chatImportRecord({}, { is_dismissed: true })).root.querySelector('[data-section="chat-import"]'),
    ).toBeNull();
  });

  it("offers nothing to an account outside the inner circle, but keeps following an import it has", () => {
    const hidden = render(chatImportRecord({}, { is_offered: false }));
    expect(hidden.root.querySelector('[data-section="chat-import"]')).toBeNull();

    const running = render(chatImportRecord({ claude: source("importing", 3) }, { is_offered: false }));
    expect(running.root.querySelector('[data-chat-import-phase="importing"]')).not.toBeNull();
  });

  it("offers the import, whose action starts the import chat and whose other puts it away", () => {
    const { root, started, dismissals } = render(chatImportRecord());
    expect(root.querySelector('[data-chat-import-phase="offer"]')).not.toBeNull();
    click(root, "import");
    expect(started).toEqual([IMPORT_PROMPT]);
    click(root, "dismiss");
    expect(dismissals).toHaveLength(1);
  });

  it("counts each source up while the import runs, with nothing to click", () => {
    const { root } = render(chatImportRecord({ chatgpt: source("importing", 12), claude: source("imported", 30) }));
    expect(root.querySelector('[data-chat-import-phase="importing"]')).not.toBeNull();
    expect(Array.from(root.querySelectorAll("[data-chat-import-source]")).map((line) => line.textContent)).toEqual([
      "Claude: 30 imported",
      "ChatGPT: 12 so far",
    ]);
    expect(root.querySelector("[data-chat-import-action]")).toBeNull();
  });

  it("shows how many of its conversations a source has fetched, with a bar, when it reports a total", () => {
    const { root } = render(
      chatImportRecord({
        chatgpt: source("importing", 40, { fetched: 166, to_fetch: 582 }),
        claude: source("importing", 7),
      }),
    );
    expect(Array.from(root.querySelectorAll("[data-chat-import-source]")).map((line) => line.textContent)).toEqual([
      "Claude: 7 so far",
      "ChatGPT: 166 of 582",
    ]);
    const bar = root.querySelector<HTMLElement>('[data-chat-import-bar="chatgpt"] > div')!;
    expect(bar.style.width).toBe("28.5%");
    expect(root.querySelector('[data-chat-import-bar="claude"]')).toBeNull();
  });

  it("counts conversations downloaded instead when a source has nothing new to fetch", () => {
    const { root } = render(chatImportRecord({ chatgpt: source("importing", 312, { fetched: 0, to_fetch: 0 }) }));
    expect(root.querySelector('[data-chat-import-source="chatgpt"]')!.textContent).toBe("ChatGPT: 312 so far");
    expect(root.querySelector("[data-chat-import-bar]")).toBeNull();
  });

  it("says which source needs the user and resumes the import of that source from a chat", () => {
    const { root, started } = render(
      chatImportRecord({ claude: source("imported", 30), chatgpt: source("needs_sign_in") }),
    );
    expect(root.querySelector('[data-chat-import-source="chatgpt"]')!.textContent).toBe(
      "ChatGPT: needs you to sign in again",
    );
    click(root, "resume");
    expect(started).toEqual([
      "My chat import did not finish. Pick it up and bring the rest of my ChatGPT chats into this workspace.",
    ]);
  });

  it("shows what came in and offers to look for new chats", () => {
    const { root, started } = render(
      chatImportRecord({ claude: source("imported", 30), chatgpt: source("imported", 4) }),
    );
    expect(root.querySelector('[data-section="chat-import"]')!.textContent).toContain(
      "30 from Claude and 4 from ChatGPT.",
    );
    click(root, "update");
    expect(started).toEqual([
      "Check my Claude and ChatGPT chats for new conversations and bring them into this workspace.",
    ]);
  });

  it("looks for new chats only in the sources that were imported", () => {
    const { root, started } = render(chatImportRecord({ claude: source("imported", 30) }));
    click(root, "update");
    expect(started).toEqual(["Check my Claude chats for new conversations and bring them into this workspace."]);
  });

  it("looks for the import its action starts, since the chat takes the focus", async () => {
    vi.useFakeTimers();
    vi.resetModules();
    const fetched = vi.fn(async () => new Response(JSON.stringify(chatImportRecord()), { status: 200 }));
    vi.stubGlobal("fetch", fetched);
    const model = await import("../models/ChatImport");
    const card = await import("./ChatImportCard");
    await model.refreshChatImport();
    const root = mountView(() =>
      m(card.ChatImportCard, { chatImport: model.getChatImport(), onStartWithText: () => {}, onDismiss: () => {} }),
    );

    click(root, "import");
    await vi.advanceTimersByTimeAsync(model.POLL_INTERVAL_MS);

    expect(fetched).toHaveBeenCalledTimes(2);
  });
});
