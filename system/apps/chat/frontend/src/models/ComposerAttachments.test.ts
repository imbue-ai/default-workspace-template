// @vitest-environment jsdom
/**
 * The staged attachments outlive the document: a ready chip is persisted beside the draft text
 * and comes back after a reload, an in-flight or failed one does not, and a list another document
 * of the origin wrote (a ``storage`` event) is taken in without dropping this document's uploads.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

import {
  clearComposerAttachments,
  getComposerAttachments,
  removeComposerAttachment,
  uploadDescribedFileToComposer,
  uploadFilesToComposer,
} from "./ComposerAttachments";

const STORAGE_KEY_PREFIX = "composer-attachments:";

let chatId: string;
let uploadAnswers: (() => Promise<Response>)[];

beforeEach(() => {
  chatId = `chat-${Math.random().toString(36).slice(2)}`;
  uploadAnswers = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init: RequestInit) => {
      const answer = uploadAnswers.shift();
      if (answer !== undefined) return answer();
      const file = (init.body as FormData).get("file") as File;
      return new Response(JSON.stringify({ path: `/w/data/uploads/x/${file.name}`, size: 3 }), { status: 201 });
    }),
  );
});

afterEach(() => {
  clearComposerAttachments(chatId);
  vi.unstubAllGlobals();
});

async function settle(): Promise<void> {
  await Promise.all(getComposerAttachments(chatId).map((attachment) => attachment.uploadSettled));
}

function stored(): unknown[] | null {
  const raw = localStorage.getItem(`${STORAGE_KEY_PREFIX}${chatId}`);
  return raw === null ? null : (JSON.parse(raw) as unknown[]);
}

describe("persistence", () => {
  it("persists a ready attachment, with its summary, and not one still uploading or failed", async () => {
    uploadAnswers.push(() => Promise.resolve(new Response("nope", { status: 500 })));
    uploadFilesToComposer(chatId, [new File(["bad"], "bad.txt")]);
    uploadDescribedFileToComposer(chatId, new File(["{}"], "REF-abcdefghijk.json"), "p#para in docs /");
    expect(stored()).toBeNull();
    await settle();
    const [failed, ready] = getComposerAttachments(chatId);
    expect(failed.status).toBe("error");
    expect(ready.status).toBe("ready");
    expect(stored()).toEqual([
      {
        localId: ready.localId,
        fileName: "REF-abcdefghijk.json",
        isImage: false,
        summary: "p#para in docs /",
        uploaded: ready.uploaded,
      },
    ]);
    removeComposerAttachment(chatId, ready.localId);
    expect(stored()).toBeNull();
  });

  it("brings a stored list back as ready attachments for a chat this document has not seen", () => {
    const otherChatId = `chat-${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(
      `${STORAGE_KEY_PREFIX}${otherChatId}`,
      JSON.stringify([
        {
          localId: "composer-att-900",
          fileName: "plan.pdf",
          isImage: false,
          uploaded: { path: "/w/data/uploads/y/plan.pdf", name: "plan.pdf", size: 3, isImage: false, url: "/u" },
        },
      ]),
    );
    const [restored] = getComposerAttachments(otherChatId);
    expect(restored.status).toBe("ready");
    expect(restored.fileName).toBe("plan.pdf");
    expect(restored.uploaded?.path).toBe("/w/data/uploads/y/plan.pdf");
    // An id minted here is its own, clear of the restored one.
    uploadFilesToComposer(otherChatId, [new File(["x"], "next.txt")]);
    const ids = getComposerAttachments(otherChatId).map((attachment) => attachment.localId);
    expect(new Set(ids).size).toBe(2);
    expect(ids[1].startsWith("composer-att-")).toBe(true);
    clearComposerAttachments(otherChatId);
  });

  it("takes in a list another document wrote, keeping this document's upload in flight", async () => {
    let finish: (() => void) | null = null;
    uploadAnswers.push(
      () =>
        new Promise<Response>((resolve) => {
          finish = () =>
            resolve(new Response(JSON.stringify({ path: "/w/data/uploads/z/mine.txt", size: 3 }), { status: 201 }));
        }),
    );
    uploadFilesToComposer(chatId, [new File(["abc"], "mine.txt")]);
    const key = `${STORAGE_KEY_PREFIX}${chatId}`;
    const fromElsewhere = [
      {
        localId: "composer-att-5000",
        fileName: "REF-zzzzzzzzzzz.json",
        isImage: false,
        summary: "button#save in docs /",
        uploaded: { path: "/w/data/uploads/r/REF-zzzzzzzzzzz.json", name: "r", size: 3, isImage: false, url: "/r" },
      },
    ];
    localStorage.setItem(key, JSON.stringify(fromElsewhere));
    window.dispatchEvent(new StorageEvent("storage", { key, newValue: JSON.stringify(fromElsewhere) }));
    expect(getComposerAttachments(chatId).map((attachment) => [attachment.fileName, attachment.status])).toEqual([
      ["REF-zzzzzzzzzzz.json", "ready"],
      ["mine.txt", "uploading"],
    ]);
    (finish as (() => void) | null)?.();
    await settle();
    expect(getComposerAttachments(chatId).map((attachment) => attachment.status)).toEqual(["ready", "ready"]);
    expect(stored()).toHaveLength(2);
  });
});
