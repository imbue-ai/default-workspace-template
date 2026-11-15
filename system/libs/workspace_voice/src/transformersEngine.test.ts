import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";
import { engineSpec, engineVariant } from "./engines";

const h = vi.hoisted(() => ({
  pipelineMock: vi.fn(),
  transcribeMock: vi.fn(),
  disposeMock: vi.fn(),
  env: {
    allowLocalModels: true,
    allowRemoteModels: true,
    useBrowserCache: true,
    useCustomCache: false,
    remoteHost: "https://huggingface.co/",
    remotePathTemplate: "{model}/resolve/{revision}/",
    backends: { onnx: { wasm: {} as { wasmPaths?: string; numThreads?: number; proxy?: boolean } } },
  },
}));

vi.mock("@huggingface/transformers", () => ({ pipeline: h.pipelineMock, env: h.env }));

const SPEC = engineSpec("moonshine-base");
const VARIANT = engineVariant(SPEC, "wasm")!;
const ASSET_BASE = "https://chat.test/assets/voice/";

let loadTransformersEngine: typeof import("./transformersEngine").loadTransformersEngine;
let installPinnedFileServer: typeof import("./transformersEngine").installPinnedFileServer;
let PINNED_FILE_HOST: string;
let baseFetch: Mock;

beforeEach(async () => {
  vi.resetModules();
  vi.resetAllMocks();
  const transcriber = Object.assign(h.transcribeMock, { dispose: h.disposeMock });
  h.pipelineMock.mockResolvedValue(transcriber);
  h.transcribeMock.mockResolvedValue({ text: "  hello there  " });
  h.env.backends = { onnx: { wasm: {} } };
  baseFetch = vi.fn(async () => new Response("passed through", { status: 200 }));
  vi.stubGlobal("fetch", baseFetch);
  ({ loadTransformersEngine, installPinnedFileServer, PINNED_FILE_HOST } = await import("./transformersEngine"));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("installPinnedFileServer", () => {
  it("answers the runtime's requests for the repository's files from the checked bytes, and 404 for others", async () => {
    const files = new Map([["config.json", new TextEncoder().encode("{}").buffer as ArrayBuffer]]);
    const restore = installPinnedFileServer(SPEC.repo, files);
    const hit = await fetch(`${PINNED_FILE_HOST}${SPEC.repo}/resolve/main/config.json`);
    expect(hit.status).toBe(200);
    expect(await hit.text()).toBe("{}");
    const miss = await fetch(`${PINNED_FILE_HOST}${SPEC.repo}/resolve/main/processor_config.json`);
    expect(miss.status).toBe(404);
    const elsewhere = await fetch("https://chat.test/assets/voice/transformers-ort/ort.wasm");
    expect(await elsewhere.text()).toBe("passed through");
    expect(baseFetch).toHaveBeenCalledTimes(1);
    restore();
    expect(globalThis.fetch).toBe(baseFetch);
  });
});

describe("loadTransformersEngine", () => {
  it("loads the variant with the runtime's caches off and its WebAssembly served by the page", async () => {
    const files = new Map([["config.json", new ArrayBuffer(2)]]);
    await loadTransformersEngine(SPEC, VARIANT, files, { assetBase: ASSET_BASE });
    expect(h.pipelineMock).toHaveBeenCalledWith("automatic-speech-recognition", SPEC.repo, {
      dtype: "q8",
      device: "wasm",
    });
    expect(h.env.allowLocalModels).toBe(false);
    expect(h.env.useBrowserCache).toBe(false);
    expect(h.env.useCustomCache).toBe(false);
    expect(h.env.remoteHost).toBe(PINNED_FILE_HOST);
    expect(h.env.backends.onnx.wasm).toEqual({
      wasmPaths: `${ASSET_BASE}transformers-ort/`,
      numThreads: 1,
      proxy: false,
    });
    // The server is up only while the model loads.
    expect(globalThis.fetch).toBe(baseFetch);
  });

  it("serves the runtime the checked bytes while it loads", async () => {
    const files = new Map([["config.json", new TextEncoder().encode('{"ok":1}').buffer as ArrayBuffer]]);
    h.pipelineMock.mockImplementation(async () => {
      const response = await fetch(`${PINNED_FILE_HOST}${SPEC.repo}/resolve/main/config.json`);
      expect(await response.text()).toBe('{"ok":1}');
      return Object.assign(h.transcribeMock, { dispose: h.disposeMock });
    });
    await loadTransformersEngine(SPEC, VARIANT, files, { assetBase: ASSET_BASE });
    expect(baseFetch).not.toHaveBeenCalled();
  });

  it("caps the decoder for the audio's length and trims the text", async () => {
    const engine = await loadTransformersEngine(SPEC, VARIANT, new Map(), { assetBase: ASSET_BASE });
    const samples = new Float32Array(16000 * 2);
    await expect(engine.transcribe(samples)).resolves.toEqual({ text: "hello there", words: null });
    expect(h.transcribeMock).toHaveBeenCalledWith(samples, { max_new_tokens: 23 });
    await engine.dispose();
    expect(h.disposeMock).toHaveBeenCalledTimes(1);
  });

  it("takes the first output when the runtime answers a list", async () => {
    h.transcribeMock.mockResolvedValue([{ text: "first" }, { text: "second" }]);
    const engine = await loadTransformersEngine(SPEC, VARIANT, new Map(), { assetBase: ASSET_BASE });
    await expect(engine.transcribe(new Float32Array(160))).resolves.toEqual({ text: "first", words: null });
  });
});
