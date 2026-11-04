/**
 * The engine worker's side of the protocol (``workerProtocol.ts``): it holds one loaded engine for every page of
 * the workspace that connects, downloads the engine's pinned files once, picks the device, transcribes one job
 * at a time in the order jobs arrive, and releases the engine after it has sat idle. Pure of the worker scope:
 * ``engineWorker.ts`` wires ports to it, and the engine loader and the device pick are injected.
 */

import { downloadPinnedFiles, ModelDownloadError, describeError } from "./download";
import type { Device, EngineSpec, EngineVariant } from "./engines";
import { engineSpec, engineVariant, variantFiles } from "./engines";
import type { SpeechEngine } from "./speechEngine";
import type { PageMessage, WorkerMessage } from "./workerProtocol";

/** What a port is to the host: somewhere to post answers. */
export interface HostPort {
  postMessage(message: WorkerMessage): void;
}

export type EngineLoader = (
  spec: EngineSpec,
  variant: EngineVariant,
  files: Map<string, ArrayBuffer>,
  options: { assetBase: string },
) => Promise<SpeechEngine>;

/** The device an engine runs on here; always one the engine has a variant for. */
export type DevicePicker = (spec: EngineSpec, preferred: Device | null) => Promise<Device>;

export interface WorkerHostOptions {
  loadEngine: EngineLoader;
  pickDevice: DevicePicker;
  download?: typeof downloadPinnedFiles;
  /** How long the engine stays loaded after its last transcription. */
  idleUnloadMs?: number;
}

/** The engine stays for two minutes of silence, then goes, so a workspace that stopped dictating holds no model. */
export const DEFAULT_IDLE_UNLOAD_MS = 120_000;

interface LoadedEngine {
  spec: EngineSpec;
  device: Device;
  instance: SpeechEngine;
}

export interface WorkerHost {
  /** Register a port; answers the handler for its messages. */
  connect(port: HostPort): (message: PageMessage) => Promise<void>;
}

export function createWorkerHost(options: WorkerHostOptions): WorkerHost {
  const download = options.download ?? downloadPinnedFiles;
  const idleUnloadMs = options.idleUnloadMs ?? DEFAULT_IDLE_UNLOAD_MS;
  const ports = new Set<HostPort>();
  let loaded: LoadedEngine | null = null;
  let loading: { engineId: string; promise: Promise<LoadedEngine> } | null = null;
  // One job at a time, in arrival order across every port.
  let lane: Promise<unknown> = Promise.resolve();
  let idleTimer: ReturnType<typeof setTimeout> | null = null;

  const broadcast = (message: WorkerMessage): void => {
    for (const port of ports) port.postMessage(message);
  };

  const armIdleTimer = (): void => {
    if (idleTimer !== null) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      idleTimer = null;
      void unload();
    }, idleUnloadMs);
  };

  const unload = async (): Promise<void> => {
    const current = loaded;
    if (current === null) return;
    loaded = null;
    broadcast({ type: "unloaded" });
    try {
      await current.instance.dispose();
    } catch (error) {
      console.warn(`The speech engine could not be released cleanly: ${describeError(error)}`);
    }
  };

  const loadVariant = async (spec: EngineSpec, variant: EngineVariant, assetBase: string): Promise<SpeechEngine> => {
    const files = await download(spec, variantFiles(spec, variant), {
      onProgress: (loadedBytes, totalBytes) => broadcast({ type: "progress", loadedBytes, totalBytes }),
    });
    broadcast({ type: "loading" });
    return options.loadEngine(spec, variant, files, { assetBase });
  };

  const load = async (engineId: string, preferredDevice: Device | null, assetBase: string): Promise<LoadedEngine> => {
    const spec = engineSpec(engineId);
    if (loaded !== null) await unload();
    let device = await options.pickDevice(spec, preferredDevice);
    let variant = engineVariant(spec, device);
    if (variant === null)
      throw new Error(`${spec.displayName} has no ${device} variant; the device pick must offer one it has`);
    let instance: SpeechEngine;
    try {
      instance = await loadVariant(spec, variant, assetBase);
    } catch (error) {
      // A GPU adapter can exist and still fail to load the model; WebAssembly always works (Sculptor's fallback).
      const fallback = device === "webgpu" ? engineVariant(spec, "wasm") : null;
      if (fallback === null || error instanceof ModelDownloadError) throw error;
      console.warn(
        `${spec.displayName} did not load on WebGPU, loading on WebAssembly instead: ${describeError(error)}`,
      );
      device = "wasm";
      variant = fallback;
      instance = await loadVariant(spec, variant, assetBase);
    }
    return { spec, device, instance };
  };

  const handleLoad = async (port: HostPort, message: Extract<PageMessage, { type: "load" }>): Promise<void> => {
    if (loaded !== null && loaded.spec.id === message.engineId) {
      port.postMessage({ type: "ready", engineId: loaded.spec.id, device: loaded.device });
      armIdleTimer();
      return;
    }
    if (loading !== null && loading.engineId === message.engineId) {
      // The answer reaches every port when the load in flight settles.
      return;
    }
    const promise = load(message.engineId, message.preferredDevice, message.assetBase);
    loading = { engineId: message.engineId, promise };
    try {
      loaded = await promise;
      broadcast({ type: "ready", engineId: loaded.spec.id, device: loaded.device });
      armIdleTimer();
    } catch (error) {
      broadcast({
        type: "load-error",
        message: describeError(error),
        filePath: error instanceof ModelDownloadError ? error.file.path : null,
      });
    } finally {
      if (loading?.promise === promise) loading = null;
    }
  };

  const handleTranscribe = (port: HostPort, message: Extract<PageMessage, { type: "transcribe" }>): Promise<void> => {
    const job = async (): Promise<void> => {
      const engine = loaded;
      if (engine === null) {
        port.postMessage({ type: "transcribe-error", id: message.id, message: "The speech engine is not loaded" });
        return;
      }
      try {
        const result = await engine.instance.transcribe(new Float32Array(message.audio));
        port.postMessage({ type: "result", id: message.id, text: result.text, words: result.words });
      } catch (error) {
        port.postMessage({ type: "transcribe-error", id: message.id, message: describeError(error) });
      } finally {
        if (loaded === engine) armIdleTimer();
      }
    };
    const next = lane.then(job, job);
    lane = next;
    return next;
  };

  return {
    connect(port) {
      ports.add(port);
      return (message) => {
        switch (message.type) {
          case "load":
            return handleLoad(port, message);
          case "transcribe":
            return handleTranscribe(port, message);
        }
      };
    },
  };
}
