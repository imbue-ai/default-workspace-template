import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ModelDownloadError } from "./download";
import type { EngineSpec, PinnedFile } from "./engines";
import { engineSpec, engineVariant, variantFiles } from "./engines";
import type { SpeechEngine, TranscriptionResult } from "./speechEngine";
import { createWorkerHost } from "./workerHost";
import type { EngineLoader, HostPort } from "./workerHost";
import type { WorkerMessage } from "./workerProtocol";

const SPEC = engineSpec("moonshine-base");
const ASSET_BASE = "https://chat.test/assets/voice/";

class FakePort implements HostPort {
  readonly messages: WorkerMessage[] = [];
  postMessage(message: WorkerMessage): void {
    this.messages.push(message);
  }
  types(): string[] {
    return this.messages.map((message) => message.type);
  }
}

/** A download that answers every file at once, reporting one progress step per file. */
function instantDownload(
  spec: EngineSpec,
  files: readonly PinnedFile[],
  options: { onProgress?: (l: number, t: number) => void } = {},
) {
  const total = files.reduce((sum, file) => sum + file.bytes, 0);
  let loaded = 0;
  const map = new Map<string, ArrayBuffer>();
  for (const file of files) {
    loaded += file.bytes;
    options.onProgress?.(loaded, total);
    map.set(file.path, new ArrayBuffer(1));
  }
  return Promise.resolve(map);
}

function fakeEngine(
  transcribe = async (): Promise<TranscriptionResult> => ({ text: "words", words: null }),
): SpeechEngine {
  return { transcribe, dispose: vi.fn(async () => undefined) };
}

/** Let the host's promise chains run; the timers are fake, so this never waits on one. */
async function settled(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the engine worker host", () => {
  it("downloads the variant for the picked device, loads it, and tells every port", async () => {
    const loadEngine = vi.fn<EngineLoader>(async () => fakeEngine());
    const host = createWorkerHost({ loadEngine, pickDevice: async () => "webgpu", download: instantDownload });
    const first = new FakePort();
    const second = new FakePort();
    const handleFirst = host.connect(first);
    host.connect(second);
    await handleFirst({ type: "load", engineId: SPEC.id, preferredDevice: null, assetBase: ASSET_BASE });
    expect(loadEngine).toHaveBeenCalledTimes(1);
    const [spec, variant, files, options] = loadEngine.mock.calls[0];
    expect(variant.device).toBe("webgpu");
    expect([...files.keys()]).toEqual(variantFiles(spec, variant).map((file) => file.path));
    expect(options).toEqual({ assetBase: ASSET_BASE });
    expect(first.messages[first.messages.length - 1]).toEqual({ type: "ready", engineId: SPEC.id, device: "webgpu" });
    expect(first.types().filter((type) => type === "progress").length).toBe(variantFiles(spec, variant).length);
    expect(second.types()).toEqual(first.types());
  });

  it("answers a later load of the loaded engine at once, without downloading again", async () => {
    const loadEngine = vi.fn<EngineLoader>(async () => fakeEngine());
    const download = vi.fn(instantDownload);
    const host = createWorkerHost({ loadEngine, pickDevice: async () => "wasm", download });
    const port = new FakePort();
    const handle = host.connect(port);
    await handle({ type: "load", engineId: SPEC.id, preferredDevice: null, assetBase: ASSET_BASE });
    const later = new FakePort();
    await host.connect(later)({ type: "load", engineId: SPEC.id, preferredDevice: null, assetBase: ASSET_BASE });
    expect(download).toHaveBeenCalledTimes(1);
    expect(later.messages).toEqual([{ type: "ready", engineId: SPEC.id, device: "wasm" }]);
  });

  it("falls back to WebAssembly when the WebGPU variant does not load, and not when a download is bad", async () => {
    const loadEngine = vi.fn<EngineLoader>(async (_spec, variant) => {
      if (variant.device === "webgpu") throw new Error("no adapter after all");
      return fakeEngine();
    });
    const host = createWorkerHost({ loadEngine, pickDevice: async () => "webgpu", download: instantDownload });
    const port = new FakePort();
    await host.connect(port)({ type: "load", engineId: SPEC.id, preferredDevice: null, assetBase: ASSET_BASE });
    expect(loadEngine.mock.calls.map(([, variant]) => variant.device)).toEqual(["webgpu", "wasm"]);
    expect(port.messages[port.messages.length - 1]).toEqual({ type: "ready", engineId: SPEC.id, device: "wasm" });

    const badFile = engineVariant(SPEC, "webgpu")!.files[0];
    const failing = createWorkerHost({
      loadEngine: vi.fn(),
      pickDevice: async () => "webgpu",
      download: async () => {
        throw new ModelDownloadError(badFile, `${badFile.path} does not match its pinned SHA-256`);
      },
    });
    const other = new FakePort();
    await failing.connect(other)({ type: "load", engineId: SPEC.id, preferredDevice: null, assetBase: ASSET_BASE });
    expect(other.messages).toEqual([
      { type: "load-error", message: `${badFile.path} does not match its pinned SHA-256`, filePath: badFile.path },
    ]);
  });

  it("refuses a device pick the engine has no variant for", async () => {
    const host = createWorkerHost({
      loadEngine: vi.fn<EngineLoader>(),
      pickDevice: async () => "webgpu",
      download: instantDownload,
    });
    const onlyWasm = { ...SPEC, variants: SPEC.variants.filter((variant) => variant.device === "wasm") };
    vi.spyOn(await import("./engines"), "engineSpec").mockReturnValue(onlyWasm);
    const port = new FakePort();
    await host.connect(port)({ type: "load", engineId: SPEC.id, preferredDevice: null, assetBase: ASSET_BASE });
    expect(port.messages).toEqual([
      {
        type: "load-error",
        message: "Moonshine Base has no webgpu variant; the device pick must offer one it has",
        filePath: null,
      },
    ]);
    vi.restoreAllMocks();
  });

  it("transcribes one job at a time in arrival order across ports, answering each port its own", async () => {
    const pending: ((text: string) => void)[] = [];
    const engine = fakeEngine(
      () =>
        new Promise((resolve) => {
          pending.push((text) => resolve({ text, words: null }));
        }),
    );
    const host = createWorkerHost({
      loadEngine: async () => engine,
      pickDevice: async () => "wasm",
      download: instantDownload,
    });
    const first = new FakePort();
    const second = new FakePort();
    const handleFirst = host.connect(first);
    const handleSecond = host.connect(second);
    await handleFirst({ type: "load", engineId: SPEC.id, preferredDevice: null, assetBase: ASSET_BASE });
    first.messages.length = 0;
    second.messages.length = 0;
    const jobOne = handleFirst({ type: "transcribe", id: 1, audio: new Float32Array(16).buffer });
    const jobTwo = handleSecond({ type: "transcribe", id: 7, audio: new Float32Array(16).buffer });
    await settled();
    expect(pending.length).toBe(1);
    pending[0]("one");
    await jobOne;
    await settled();
    expect(pending.length).toBe(2);
    pending[1]("two");
    await jobTwo;
    expect(first.messages).toEqual([{ type: "result", id: 1, text: "one", words: null }]);
    expect(second.messages).toEqual([{ type: "result", id: 7, text: "two", words: null }]);
  });

  it("answers a transcription before any load, and one that fails, with an error", async () => {
    const host = createWorkerHost({
      loadEngine: async () =>
        fakeEngine(async () => {
          throw new Error("decoder exploded");
        }),
      pickDevice: async () => "wasm",
      download: instantDownload,
    });
    const port = new FakePort();
    const handle = host.connect(port);
    await handle({ type: "transcribe", id: 1, audio: new Float32Array(16).buffer });
    expect(port.messages).toEqual([{ type: "transcribe-error", id: 1, message: "The speech engine is not loaded" }]);
    await handle({ type: "load", engineId: SPEC.id, preferredDevice: null, assetBase: ASSET_BASE });
    port.messages.length = 0;
    await handle({ type: "transcribe", id: 2, audio: new Float32Array(16).buffer });
    expect(port.messages).toEqual([{ type: "transcribe-error", id: 2, message: "decoder exploded" }]);
  });

  it("releases the engine after it has sat idle and tells every port, so the next load downloads again", async () => {
    const engine = fakeEngine();
    const download = vi.fn(instantDownload);
    const host = createWorkerHost({
      loadEngine: async () => engine,
      pickDevice: async () => "wasm",
      download,
      idleUnloadMs: 1000,
    });
    const port = new FakePort();
    const handle = host.connect(port);
    await handle({ type: "load", engineId: SPEC.id, preferredDevice: null, assetBase: ASSET_BASE });
    await vi.advanceTimersByTimeAsync(900);
    await handle({ type: "transcribe", id: 1, audio: new Float32Array(16).buffer });
    // The transcription pushed the release back.
    await vi.advanceTimersByTimeAsync(900);
    expect(engine.dispose).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(200);
    expect(engine.dispose).toHaveBeenCalledTimes(1);
    expect(port.messages[port.messages.length - 1]).toEqual({ type: "unloaded" });
    await handle({ type: "load", engineId: SPEC.id, preferredDevice: null, assetBase: ASSET_BASE });
    expect(download).toHaveBeenCalledTimes(2);
  });
});
