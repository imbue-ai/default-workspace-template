/**
 * The spill: only an oversize reference block goes to a file, the pointer form takes its place,
 * a failed write leaves the block standing, and a text with nothing to spill costs no request.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

import {
  ELEMENT_REFERENCE_BLOCK_LIMIT,
  ELEMENT_REFERENCE_FILE_KEY,
  ELEMENT_REFERENCE_SUMMARY_KEY,
  jsonBlock,
} from "@imbue/workspace-ui/src/element_reference";
import { spillOversizeElementReferences } from "./elementReferences";

function reference(text: string): Record<string, unknown> {
  return {
    element_reference: {
      app: "docs",
      window_id: "win-1",
      page_path: "/",
      tag: "p",
      id: "para",
      selector: "#para",
      text,
    },
  };
}

const SMALL = jsonBlock(reference("short"));
const BIG = jsonBlock(reference("x".repeat(ELEMENT_REFERENCE_BLOCK_LIMIT)));

let fetchSpy: ReturnType<typeof vi.fn>;

function stubFetch(answer: () => Promise<Response>): void {
  fetchSpy = vi.fn(answer);
  vi.stubGlobal("fetch", fetchSpy);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("spillOversizeElementReferences", () => {
  it("leaves a text with no oversize block alone, without a request", async () => {
    stubFetch(() => Promise.reject(new Error("must not be called")));
    const text = `Explain this element:\n\n${SMALL}\n\n`;
    expect(await spillOversizeElementReferences(text)).toBe(text);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("writes an oversize block to a file and puts the pointer form in its place", async () => {
    stubFetch(() =>
      Promise.resolve(new Response(JSON.stringify({ path: "/tmp/element_references/abc.json" }), { status: 200 })),
    );
    const text = `Modify this element:\n\n${BIG}\n\n${SMALL}\n\n`;
    const spilled = await spillOversizeElementReferences(text);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/element-references");
    expect(JSON.parse(init.body as string)).toEqual({
      reference: reference("x".repeat(ELEMENT_REFERENCE_BLOCK_LIMIT)),
    });
    expect(spilled.startsWith("Modify this element:\n\n```json\n")).toBe(true);
    expect(spilled).toContain(SMALL);
    const pointerJson = spilled.split("\n")[3];
    const pointer = JSON.parse(pointerJson) as Record<string, unknown>;
    expect(pointer[ELEMENT_REFERENCE_FILE_KEY]).toBe("/tmp/element_references/abc.json");
    expect(pointer[ELEMENT_REFERENCE_SUMMARY_KEY]).toMatchObject({
      app: "docs",
      tag: "p",
      id: "para",
      selector: "#para",
    });
    expect((pointer[ELEMENT_REFERENCE_SUMMARY_KEY] as { text: string }).text).toHaveLength(200);
  });

  it("keeps the block, with a warning, when the file cannot be written", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    stubFetch(() => Promise.resolve(new Response(JSON.stringify({ detail: "disk full" }), { status: 500 })));
    const text = `Explain this element:\n\n${BIG}\n\n`;
    expect(await spillOversizeElementReferences(text)).toBe(text);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("disk full"));
    warn.mockRestore();
  });

  it("ignores an oversize block that is not a reference envelope", async () => {
    stubFetch(() => Promise.reject(new Error("must not be called")));
    const other = jsonBlock({ other: "y".repeat(ELEMENT_REFERENCE_BLOCK_LIMIT) });
    expect(await spillOversizeElementReferences(other)).toBe(other);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
