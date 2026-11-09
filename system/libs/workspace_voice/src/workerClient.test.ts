import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EngineLoadError, EngineWorkerLostError, connectEngineWorker, engineWorkerName } from "./workerClient";
import type { EngineWorkerClientOptions, EngineWorkerPort } from "./workerClient";
import type { PageMessage, WorkerMessage } from "./workerProtocol";

class FakePort implements EngineWorkerPort {
  readonly sent: { message: PageMessage; transfer: Transferable[] | undefined }[] = [];
  onmessage: ((event: MessageEvent<WorkerMessage>) => void) | null = null;
  onmessageerror: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  postMessage(message: PageMessage, transfer?: Transferable[]): void {
    this.sent.push({ message, transfer });
  }
  answer(message: WorkerMessage): void {
    this.onmessage?.({ data: message } as MessageEvent<WorkerMessage>);
  }
}

const ASSET_BASE = "https://chat.test/assets/voice/";

function connect(options: EngineWorkerClientOptions = {}) {
  const port = new FakePort();
  const client = connectEngineWorker("moonshine-base", ASSET_BASE, () => port, options);
  return { port, client };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the engine worker client", () => {
  it("asks for the engine and resolves with the device once the worker says ready, reporting progress", async () => {
    const { port, client } = connect();
    const progress: [number, number][] = [];
    const load = client.load("webgpu", (loaded, total) => progress.push([loaded, total]));
    expect(port.sent[0].message).toEqual({
      type: "load",
      engineId: "moonshine-base",
      preferredDevice: "webgpu",
      assetBase: ASSET_BASE,
    });
    port.answer({ type: "progress", loadedBytes: 10, totalBytes: 100 });
    port.answer({ type: "ready", engineId: "moonshine-base", device: "wasm" });
    await expect(load).resolves.toBe("wasm");
    expect(progress).toEqual([[10, 100]]);
  });

  it("rejects a load the worker could not do, naming the file when a download failed", async () => {
    const { port, client } = connect();
    const load = client.load(null, () => undefined);
    port.answer({
      type: "load-error",
      message: "config.json does not match its pinned SHA-256",
      filePath: "config.json",
    });
    const error = await load.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(EngineLoadError);
    expect((error as EngineLoadError).filePath).toBe("config.json");
    expect(client.isDead).toBe(false);
  });

  it("transfers the audio buffer and resolves the matching result", async () => {
    const { port, client } = connect();
    const audio = new Float32Array([1, 2, 3]);
    const buffer = audio.buffer;
    const request = client.transcribe(audio);
    const { message, transfer } = port.sent[0];
    expect(message.type).toBe("transcribe");
    expect(transfer).toEqual([buffer]);
    if (message.type !== "transcribe") throw new Error("not a transcribe");
    port.answer({ type: "result", id: message.id, text: "spoken", words: null });
    await expect(request).resolves.toEqual({ text: "spoken", words: null });
  });

  it("rejects the matching request on a transcription error without dying", async () => {
    const { port, client } = connect();
    const request = client.transcribe(new Float32Array(4));
    const message = port.sent[0].message;
    if (message.type !== "transcribe") throw new Error("not a transcribe");
    port.answer({ type: "transcribe-error", id: message.id, message: "decode failed" });
    await expect(request).rejects.toThrow("decode failed");
    expect(client.isDead).toBe(false);
  });

  it("takes the worker for dead when a job goes unanswered, failing it and everything after", async () => {
    const { client } = connect({ transcribeTimeoutMs: 1000 });
    const request = client.transcribe(new Float32Array(4));
    const settledFirst = request.catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(1000);
    expect(await settledFirst).toBeInstanceOf(EngineWorkerLostError);
    expect(client.isDead).toBe(true);
    await expect(client.transcribe(new Float32Array(4))).rejects.toBeInstanceOf(EngineWorkerLostError);
    await expect(client.load(null, () => undefined)).rejects.toBeInstanceOf(EngineWorkerLostError);
  });

  it("takes a load that goes quiet for a lost worker, but not one that keeps reporting", async () => {
    const { port, client } = connect({ loadStallTimeoutMs: 1000 });
    const load = client.load(null, () => undefined);
    const settled = load.catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(900);
    port.answer({ type: "progress", loadedBytes: 1, totalBytes: 2 });
    await vi.advanceTimersByTimeAsync(900);
    port.answer({ type: "loading" });
    await vi.advanceTimersByTimeAsync(900);
    expect(client.isDead).toBe(false);
    await vi.advanceTimersByTimeAsync(200);
    expect(await settled).toBeInstanceOf(EngineWorkerLostError);
    expect(client.isDead).toBe(true);
  });

  it("dies on the worker's own error, failing what is pending", async () => {
    const { port, client } = connect();
    const load = client.load(null, () => undefined);
    port.onerror?.({ message: "boom" } as ErrorEvent);
    await expect(load).rejects.toThrow("boom");
    expect(client.isDead).toBe(true);
  });

  it("tells its listeners when the worker released the engine, and names the worker for the engine", () => {
    const { port, client } = connect();
    const listener = vi.fn();
    client.onUnloaded(listener);
    port.answer({ type: "unloaded" });
    expect(listener).toHaveBeenCalledTimes(1);
    expect(engineWorkerName("moonshine-base")).toBe("workspace-voice:moonshine-base");
  });

  it("fails what is pending when disposed", async () => {
    const { client } = connect();
    const request = client.transcribe(new Float32Array(4));
    client.dispose();
    await expect(request).rejects.toBeInstanceOf(EngineWorkerLostError);
  });
});
