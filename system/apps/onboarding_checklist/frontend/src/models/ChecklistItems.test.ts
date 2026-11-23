import { afterEach, describe, expect, it, vi } from "vitest";
import { ChecklistReader, parseChecklistItems } from "./ChecklistItems";
import type { ChecklistItem } from "./ChecklistItems";

const NEW_CHAT: ChecklistItem = { id: "new_chat", label: "Work in parallel by starting a new chat", isDone: false };
const NEW_CHAT_DONE: ChecklistItem = { ...NEW_CHAT, isDone: true };

afterEach(() => {
  vi.restoreAllMocks();
});

/** A reader over answers handed out one per read, counting the reads and the redraws. */
function readerOver(answers: readonly (readonly ChecklistItem[] | Error)[]): {
  reader: ChecklistReader;
  reads: () => number;
  redraws: () => number;
} {
  let readCount = 0;
  let redrawCount = 0;
  const reader = new ChecklistReader({
    fetchItems: () => {
      const answer = answers[Math.min(readCount, answers.length - 1)];
      readCount += 1;
      return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
    },
    redraw: () => {
      redrawCount += 1;
    },
  });
  return { reader, reads: () => readCount, redraws: () => redrawCount };
}

/** Let every read that is under way land. */
async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe("the items an answer carries", () => {
  it("are the answer's items in its order, with the wire's is_done as isDone", () => {
    expect(
      parseChecklistItems({
        items: [
          { id: "new_chat", label: "Work in parallel by starting a new chat", is_done: true },
          { id: "build_app", label: "Build an app", is_done: false },
        ],
      }),
    ).toEqual([NEW_CHAT_DONE, { id: "build_app", label: "Build an app", isDone: false }]);
  });

  it("leave out an entry that is not shaped as an item, and are none for an answer that is not one", () => {
    expect(parseChecklistItems({ items: [null, { id: 3, label: "x" }, { id: "new_chat" }] })).toEqual([]);
    expect(parseChecklistItems({ items: "none" })).toEqual([]);
    expect(parseChecklistItems(null)).toEqual([]);
  });
});

describe("the reader of the checklist's items", () => {
  it("holds nothing until its first read answers, then the items, and redraws", async () => {
    const { reader, redraws } = readerOver([[NEW_CHAT]]);
    expect(reader.getItems()).toBeNull();

    await reader.read();

    expect(reader.getItems()).toEqual([NEW_CHAT]);
    expect(redraws()).toBe(1);
  });

  it("reads again when the shell shows the window, when the window gets focus, and on the timer's tick", async () => {
    const { reader, reads } = readerOver([[NEW_CHAT], [NEW_CHAT_DONE]]);
    await reader.read();

    reader.noteShownByShell(true);
    await settle();
    expect(reads()).toBe(2);
    expect(reader.getItems()).toEqual([NEW_CHAT_DONE]);

    reader.noteFocused();
    reader.tick();
    await settle();
    expect(reads()).toBe(4);
  });

  it("reads nothing while the shell has the window hidden, and reads once it is shown again", async () => {
    const { reader, reads } = readerOver([[NEW_CHAT]]);

    reader.noteShownByShell(false);
    reader.tick();
    reader.noteFocused();
    reader.noteDocumentVisible(true);
    await settle();
    expect(reads()).toBe(0);

    reader.noteShownByShell(true);
    await settle();
    expect(reads()).toBe(1);
  });

  it("reads nothing while the document is not visible, and reads once it is again", async () => {
    const { reader, reads } = readerOver([[NEW_CHAT]]);

    reader.noteDocumentVisible(false);
    reader.tick();
    reader.noteShownByShell(true);
    await settle();
    expect(reads()).toBe(0);

    reader.noteDocumentVisible(true);
    await settle();
    expect(reads()).toBe(1);
  });

  it("keeps the items it holds when a read fails", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { reader, redraws } = readerOver([[NEW_CHAT_DONE], new Error("the app is restarting")]);
    await reader.read();

    await reader.read();

    expect(reader.getItems()).toEqual([NEW_CHAT_DONE]);
    expect(redraws()).toBe(1);
  });

  it("drops an answer that lands after a later read's", async () => {
    const resolvers: ((items: readonly ChecklistItem[]) => void)[] = [];
    const reader = new ChecklistReader({
      fetchItems: () => new Promise((resolve) => resolvers.push(resolve)),
      redraw: () => undefined,
    });
    const first = reader.read();
    const second = reader.read();

    resolvers[1]([NEW_CHAT_DONE]);
    resolvers[0]([NEW_CHAT]);
    await Promise.all([first, second]);

    expect(reader.getItems()).toEqual([NEW_CHAT_DONE]);
  });
});
