// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import type { ChatImport } from "../models/ChatImport";
import { chatImportSourceRecord as source } from "../testing/records";
import { ChatImportCard, IMPORT_PROMPT } from "./ChatImportCard";

afterEach(() => unmountViews());

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
    expect(render({ is_dismissed: true, sources: {} }).root.querySelector('[data-section="chat-import"]')).toBeNull();
  });

  it("offers the import, whose action starts the import chat and whose other puts it away", () => {
    const { root, started, dismissals } = render({ is_dismissed: false, sources: {} });
    expect(root.querySelector('[data-chat-import-phase="offer"]')).not.toBeNull();
    click(root, "import");
    expect(started).toEqual([IMPORT_PROMPT]);
    click(root, "dismiss");
    expect(dismissals).toHaveLength(1);
  });

  it("counts each source up while the import runs, with nothing to click", () => {
    const { root } = render({
      is_dismissed: false,
      sources: { chatgpt: source("importing", 12), claude: source("imported", 30) },
    });
    expect(root.querySelector('[data-chat-import-phase="importing"]')).not.toBeNull();
    expect(Array.from(root.querySelectorAll("[data-chat-import-source]")).map((line) => line.textContent)).toEqual([
      "Claude: 30 imported",
      "ChatGPT: 12 so far",
    ]);
    expect(root.querySelector("[data-chat-import-action]")).toBeNull();
  });

  it("says which source needs the user and resumes the import of that source from a chat", () => {
    const { root, started } = render({
      is_dismissed: false,
      sources: { claude: source("imported", 30), chatgpt: source("needs_sign_in") },
    });
    expect(root.querySelector('[data-chat-import-source="chatgpt"]')!.textContent).toBe(
      "ChatGPT: needs you to sign in again",
    );
    click(root, "resume");
    expect(started).toEqual([
      "My chat import did not finish. Pick it up and bring the rest of my ChatGPT chats into this workspace.",
    ]);
  });

  it("shows what came in and offers to look for new chats", () => {
    const { root, started } = render({
      is_dismissed: false,
      sources: { claude: source("imported", 30), chatgpt: source("imported", 4) },
    });
    expect(root.querySelector('[data-section="chat-import"]')!.textContent).toContain(
      "30 from Claude and 4 from ChatGPT.",
    );
    click(root, "update");
    expect(started).toEqual([
      "Check my Claude and ChatGPT chats for new conversations and bring them into this workspace.",
    ]);
  });

  it("looks for new chats only in the sources that were imported", () => {
    const { root, started } = render({ is_dismissed: false, sources: { claude: source("imported", 30) } });
    click(root, "update");
    expect(started).toEqual(["Check my Claude chats for new conversations and bring them into this workspace."]);
  });
});
