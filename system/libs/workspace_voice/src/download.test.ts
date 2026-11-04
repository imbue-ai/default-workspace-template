import { describe, expect, it } from "vitest";
import { ModelDownloadError, downloadPinnedFiles } from "./download";
import type { EngineSpec, PinnedFile } from "./engines";
import { engineSpec } from "./engines";

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
