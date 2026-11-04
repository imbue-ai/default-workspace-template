/**
 * A page's handle on the engine worker (``workerProtocol.ts``): connect to the workspace's worker for an engine,
 * ask for the engine to be loaded, and send it audio one job at a time. The worker is a SharedWorker, so every
 * chat page of the workspace reaches the same loaded model; where the browser has none (Chrome on Android), each
 * page owns a dedicated worker instead, and only a page that dictates loads one.
 */

import type { Device, EngineId } from "./engines";
import type { TranscriptionResult } from "./speechEngine";
import type { PageMessage, WorkerMessage } from "./workerProtocol";

/** The part of a MessagePort (or a dedicated Worker) the client uses. */
export interface EngineWorkerPort {
  postMessage(message: PageMessage, transfer?: Transferable[]): void;
  onmessage: ((event: MessageEvent<WorkerMessage>) => void) | null;
  onmessageerror?: ((event: MessageEvent) => void) | null;
}

export type PortFactory = (engineId: EngineId) => EngineWorkerPort;

/** The worker went away mid-job: nothing answered. The client is dead; the next start connects again. */
export class EngineWorkerLostError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "EngineWorkerLostError";
  }
}

export class EngineLoadError extends Error {
  readonly filePath: string | null;

  constructor(message: string, filePath: string | null) {
    super(message);
    this.name = "EngineLoadError";
    this.filePath = filePath;
  }
}

export interface EngineWorkerClient {
  readonly engineId: EngineId;
  readonly isDead: boolean;
  /** Have the worker load the engine (a download the first time); resolves with the device it runs on. */
  load(preferredDevice: Device | null, onProgress: (loadedBytes: number, totalBytes: number) => void): Promise<Device>;
  transcribe(audio: Float32Array): Promise<TranscriptionResult>;
  /** Called when the worker released the engine after sitting idle. */
  onUnloaded(listener: () => void): void;
  /** Stop listening to the worker; pending jobs fail as lost. */
  dispose(): void;
}

export interface EngineWorkerClientOptions {
  /** How long a job may go unanswered before the worker is taken for dead. Inference on a slow machine can take
   *  longer than the audio it covers, so this is generous. */
  transcribeTimeoutMs?: number;
}

export const DEFAULT_TRANSCRIBE_TIMEOUT_MS = 120_000;

interface PendingTranscription {
  resolve(result: TranscriptionResult): void;
  reject(error: Error): void;
  timer: ReturnType<typeof setTimeout>;
}

interface PendingLoad {
  resolve(device: Device): void;
  reject(error: Error): void;
  onProgress(loadedBytes: number, totalBytes: number): void;
}

export function connectEngineWorker(
  engineId: EngineId,
  assetBase: string,
  createPort: PortFactory,
  options: EngineWorkerClientOptions = {},
): EngineWorkerClient {
  const timeoutMs = options.transcribeTimeoutMs ?? DEFAULT_TRANSCRIBE_TIMEOUT_MS;
  const port = createPort(engineId);
  const pending = new Map<number, PendingTranscription>();
  const unloadedListeners: (() => void)[] = [];
  let pendingLoads: PendingLoad[] = [];
  let nextId = 0;
  let isDead = false;

  const die = (error: Error): void => {
    if (isDead) return;
    isDead = true;
    for (const job of pending.values()) {
      clearTimeout(job.timer);
      job.reject(error);
    }
    pending.clear();
    for (const load of pendingLoads) load.reject(error);
    pendingLoads = [];
    port.onmessage = null;
  };

  port.onmessage = (event) => {
    const message = event.data;
    switch (message.type) {
      case "progress":
        for (const load of pendingLoads) load.onProgress(message.loadedBytes, message.totalBytes);
        break;
      case "ready": {
        const loads = pendingLoads;
        pendingLoads = [];
        for (const load of loads) load.resolve(message.device);
        break;
      }
      case "load-error": {
        const loads = pendingLoads;
        pendingLoads = [];
        for (const load of loads) load.reject(new EngineLoadError(message.message, message.filePath));
        break;
      }
      case "unloaded":
        for (const listener of unloadedListeners) listener();
        break;
      case "result": {
        const job = pending.get(message.id);
        if (job === undefined) break;
        pending.delete(message.id);
        clearTimeout(job.timer);
        job.resolve({ text: message.text, words: message.words });
        break;
      }
      case "transcribe-error": {
        const job = pending.get(message.id);
        if (job === undefined) break;
        pending.delete(message.id);
        clearTimeout(job.timer);
        job.reject(new Error(message.message));
        break;
      }
    }
  };
  port.onmessageerror = () => die(new EngineWorkerLostError("The speech engine sent something unreadable"));

  return {
    engineId,
    get isDead() {
      return isDead;
    },
    load(preferredDevice, onProgress) {
      if (isDead) return Promise.reject(new EngineWorkerLostError("The speech engine worker is gone"));
      return new Promise<Device>((resolve, reject) => {
        pendingLoads.push({ resolve, reject, onProgress });
        port.postMessage({ type: "load", engineId, preferredDevice, assetBase });
      });
    },
    transcribe(audio) {
      if (isDead) return Promise.reject(new EngineWorkerLostError("The speech engine worker is gone"));
      nextId += 1;
      const id = nextId;
      return new Promise<TranscriptionResult>((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          die(new EngineWorkerLostError("The speech engine stopped answering"));
          reject(new EngineWorkerLostError("The speech engine stopped answering"));
        }, timeoutMs);
        pending.set(id, { resolve, reject, timer });
        // The buffer moves to the worker rather than being copied; the caller's view is spent.
        port.postMessage({ type: "transcribe", id, audio: audio.buffer as ArrayBuffer }, [
          audio.buffer as ArrayBuffer,
        ]);
      });
    },
    onUnloaded(listener) {
      unloadedListeners.push(listener);
    },
    dispose() {
      die(new EngineWorkerLostError("The page let go of the speech engine"));
    },
  };
}

/** The worker's name: one per workspace origin and engine, so every page shares one loaded model. */
export function engineWorkerName(engineId: EngineId): string {
  return `workspace-voice:${engineId}`;
}

/** The page's port to the workspace's engine worker: a SharedWorker where the browser has them, else a worker of
 *  the page's own. The worker's URL is a literal here so the bundler emits it as a chunk of its own. */
export function openEngineWorkerPort(engineId: EngineId): EngineWorkerPort {
  if (typeof SharedWorker === "function") {
    const worker = new SharedWorker(new URL("./engineWorker.ts", import.meta.url), {
      type: "module",
      name: engineWorkerName(engineId),
    });
    worker.port.start();
    return worker.port;
  }
  return new Worker(new URL("./engineWorker.ts", import.meta.url), { type: "module" });
}
