import { describe, expect, it, vi } from "vitest";
import { ModelDownloadError, downloadPinnedFiles, isPinnedFile } from "./download";
import type { EngineSpec, PinnedFile } from "./engines";
import { engineSpec } from "./engines";
import type { PinnedFileStore } from "./fileStore";

const SPEC: EngineSpec = engineSpec("moonshine-base");

async function pinned(path: string, content: string): Promise<{ file: PinnedFile; body: Uint8Array }> {
  const body = new TextEncoder().encode(content);
  const digest = await crypto.subtle.digest("SHA-256", body);
  const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  return { file: { path, sha256, bytes: body.length }, body };
}

/** A fetch answering each URL from ``bodies``, streamed in two chunks so progress is reported in between. */
function fetchFrom(bodies: Map<string, Uint8Array>, requested: string[] = []) {
  return async (url: string): Promise<Response> => {
    requested.push(url);
    const body = bodies.get(url);
    if (body === undefined) return new Response(null, { status: 404 });
    const half = Math.ceil(body.length / 2);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(body.slice(0, half));
        controller.enqueue(body.slice(half));
        controller.close();
      },
    });
    return new Response(stream, { status: 200 });
  };
}

function urlOf(file: PinnedFile): string {
  return `https://huggingface.co/${SPEC.repo}/resolve/${SPEC.commit}/${file.path}`;
}

/** A store over a map, with every call recorded. */
function mapStore(initial: [string, Uint8Array][] = []) {
  const entries = new Map<string, ArrayBuffer>(
    initial.map(([url, bytes]) => [url, bytes.slice().buffer as ArrayBuffer]),
  );
  const store: PinnedFileStore = {
    read: vi.fn(async (url: string) => entries.get(url) ?? null),
    write: vi.fn(async (url: string, bytes: ArrayBuffer) => void entries.set(url, bytes)),
    prune: vi.fn(async () => undefined),
  };
  return { store, entries };
}

describe("downloadPinnedFiles", () => {
  it("downloads each file at the engine's commit, reports progress, and answers the verified bytes", async () => {
    const first = await pinned("config.json", '{"a": 1}');
    const second = await pinned("onnx/encoder.onnx", "weights go here");
    const requested: string[] = [];
    const progress: [number, number][] = [];
    const files = await downloadPinnedFiles(SPEC, [first.file, second.file], {
      fetch: fetchFrom(
        new Map([
          [urlOf(first.file), first.body],
          [urlOf(second.file), second.body],
        ]),
        requested,
      ),
      onProgress: (loaded, total) => progress.push([loaded, total]),
    });
    expect(requested).toEqual([urlOf(first.file), urlOf(second.file)]);
    expect(new TextDecoder().decode(files.get("config.json"))).toBe('{"a": 1}');
    expect(new TextDecoder().decode(files.get("onnx/encoder.onnx"))).toBe("weights go here");
    const total = first.file.bytes + second.file.bytes;
    expect(progress[progress.length - 1]).toEqual([total, total]);
    expect(progress.every(([loaded, each]) => loaded <= each && each === total)).toBe(true);
    expect(progress.length).toBeGreaterThan(2);
  });

  it("refuses a file whose bytes do not hash to the pin, naming it", async () => {
    const { file, body } = await pinned("tokenizer.json", "the real file");
    const tampered = { ...file, sha256: "0".repeat(64) };
    await expect(
      downloadPinnedFiles(SPEC, [tampered], { fetch: fetchFrom(new Map([[urlOf(tampered), body]])) }),
    ).rejects.toMatchObject({ name: "ModelDownloadError", file: tampered, message: /tokenizer.json.*SHA-256/ });
  });

  it("refuses a file that is not its pinned size, whether short or long", async () => {
    const { file, body } = await pinned("config.json", "twelve bytes");
    const short = { ...file, bytes: file.bytes + 1 };
    await expect(
      downloadPinnedFiles(SPEC, [short], { fetch: fetchFrom(new Map([[urlOf(short), body]])) }),
    ).rejects.toThrow(/config.json is 12 bytes, not its pinned 13/);
    const long = { ...file, bytes: file.bytes - 1 };
    await expect(
      downloadPinnedFiles(SPEC, [long], { fetch: fetchFrom(new Map([[urlOf(long), body]])) }),
    ).rejects.toThrow(/longer than its pinned 11 bytes/);
  });

  it("takes a kept file that checks out without fetching, and keeps what it downloads", async () => {
    const kept = await pinned("config.json", '{"kept": true}');
    const fresh = await pinned("tokenizer.json", "downloaded");
    const { store, entries } = mapStore([[urlOf(kept.file), kept.body]]);
    const requested: string[] = [];
    const progress: [number, number][] = [];
    const files = await downloadPinnedFiles(SPEC, [kept.file, fresh.file], {
      store,
      fetch: fetchFrom(new Map([[urlOf(fresh.file), fresh.body]]), requested),
      onProgress: (loaded, total) => progress.push([loaded, total]),
    });
    expect(requested).toEqual([urlOf(fresh.file)]);
    expect(new TextDecoder().decode(files.get("config.json"))).toBe('{"kept": true}');
    expect(new TextDecoder().decode(entries.get(urlOf(fresh.file)))).toBe("downloaded");
    // The kept file counts as loaded at once.
    expect(progress[0]).toEqual([kept.file.bytes, kept.file.bytes + fresh.file.bytes]);
  });

  it("downloads afresh over a kept file that does not check out", async () => {
    const { file, body } = await pinned("config.json", "the real file");
    const { store, entries } = mapStore([[urlOf(file), new TextEncoder().encode("the real fil")]]);
    const requested: string[] = [];
    await downloadPinnedFiles(SPEC, [file], { store, fetch: fetchFrom(new Map([[urlOf(file), body]]), requested) });
    expect(requested).toEqual([urlOf(file)]);
    expect(new TextDecoder().decode(entries.get(urlOf(file)))).toBe("the real file");
  });

  it("checks a file's size and hash", async () => {
    const { file, body } = await pinned("config.json", "exact");
    expect(await isPinnedFile(file, body.buffer as ArrayBuffer)).toBe(true);
    expect(await isPinnedFile(file, new TextEncoder().encode("exacu").buffer as ArrayBuffer)).toBe(false);
    expect(await isPinnedFile(file, new TextEncoder().encode("exact!").buffer as ArrayBuffer)).toBe(false);
  });

  it("reports an unreachable host and an HTTP failure as a download error naming the file", async () => {
    const { file } = await pinned("config.json", "x");
    await expect(
      downloadPinnedFiles(SPEC, [file], {
        fetch: async () => {
          throw new TypeError("Failed to fetch");
        },
      }),
    ).rejects.toThrow(/config.json could not be downloaded: Failed to fetch/);
    const error = await downloadPinnedFiles(SPEC, [file], { fetch: fetchFrom(new Map()) }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ModelDownloadError);
    expect((error as Error).message).toMatch(/HTTP 404/);
  });
});
