// @vitest-environment jsdom
/**
 * A reference block entering a composer becomes an attachment: the block leaves the text, a
 * ``REF-<id>.json`` file goes up the ordinary upload path with the reference's summary on its chip,
 * a text with no reference block costs nothing, and a block that is not a reference is left alone.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

import { jsonBlock, type ElementReference } from "@imbue/workspace-ui/src/element_reference";
import { clearComposerAttachments, getComposerAttachments } from "./ComposerAttachments";
import { referenceFileOf, stageElementReferences } from "./elementReferences";

function reference(id: string): ElementReference {
  return {
    reference_id: id,
    app: "docs",
    window_id: "win-1",
    desktop_id: "home",
    client_id: "client-1",
    page_origin: "http://docs.test",
    page_path: "/intro",
    page_title: "Intro",
    viewport: { width: 800, height: 600 },
    pointer: { client_x: 1, client_y: 2, page_x: 1, page_y: 2 },
    tag: "p",
    id: "para",
    classes: ["lead"],
    attributes: {},
    role: null,
    aria_label: null,
    selection_text: "",
    selection_box: null,
    input_value: null,
    link_href: null,
    image_src: null,
    selector: "#para",
    bounding_box: { x: 0, y: 0, width: 10, height: 10 },
  };
}

const ID_A = "REF-aaaaaaaaaaa";
const ID_B = "REF-bbbbbbbbbbb";
const BLOCK_A = jsonBlock({ element_reference: reference(ID_A) });
const BLOCK_B = jsonBlock({ element_reference: reference(ID_B) });

let fetchSpy: ReturnType<typeof vi.fn>;
let chatId: string;

beforeEach(() => {
  chatId = `chat-${Math.random().toString(36).slice(2)}`;
  fetchSpy = vi.fn(async (_url: string, init: RequestInit) => {
    const file = (init.body as FormData).get("file") as File;
    return new Response(JSON.stringify({ path: `/w/data/uploads/x/${file.name}`, size: file.size }), { status: 201 });
  });
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  clearComposerAttachments(chatId);
  vi.unstubAllGlobals();
});

describe("referenceFileOf", () => {
  it("names the file after the id and holds the envelope pretty-printed", async () => {
    const file = referenceFileOf({ element_reference: reference(ID_A) });
    expect(file.name).toBe(`${ID_A}.json`);
    expect(file.type).toBe("application/json");
    const text = await file.text();
    expect(text.split("\n").length).toBeGreaterThan(10);
    expect(JSON.parse(text)).toEqual({ element_reference: reference(ID_A) });
  });
});

describe("stageElementReferences", () => {
  it("leaves a text with no reference block alone, uploading nothing", () => {
    const text = 'Explain what I attached in REF-x\n\n```json\n{"other": 1}\n```';
    expect(stageElementReferences(chatId, text)).toBe(text);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(getComposerAttachments(chatId)).toEqual([]);
  });

  it("takes each block out of the text and uploads it as a file with the reference's summary", async () => {
    const text = `Change ${ID_A} to \n\n${BLOCK_A}\n\nAnd ${ID_B}:\n\n${BLOCK_B}`;
    expect(stageElementReferences(chatId, text)).toBe(`Change ${ID_A} to \n\nAnd ${ID_B}:`);
    const staged = getComposerAttachments(chatId);
    expect(staged.map((attachment) => attachment.fileName)).toEqual([`${ID_A}.json`, `${ID_B}.json`]);
    expect(staged[0].summary).toBe("p#para.lead in docs /intro");
    expect(staged[0].isImage).toBe(false);
    await Promise.all(staged.map((attachment) => attachment.uploadSettled));
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(fetchSpy.mock.calls[0][0]).toBe("/api/uploads");
    const ready = getComposerAttachments(chatId);
    expect(ready.map((attachment) => attachment.status)).toEqual(["ready", "ready"]);
    expect(ready[0].uploaded?.path).toBe(`/w/data/uploads/x/${ID_A}.json`);
  });

  it("answers an empty text for a block on its own", () => {
    expect(stageElementReferences(chatId, BLOCK_A)).toBe("");
    expect(getComposerAttachments(chatId)).toHaveLength(1);
  });
});
