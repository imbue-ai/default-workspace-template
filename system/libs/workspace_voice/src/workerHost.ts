/**
 * The engine worker's side of the protocol (``workerProtocol.ts``): it holds one loaded engine for every page of
 * the workspace that connects, gets the engine's pinned files (from the store the last load kept them in, else
 * downloaded and kept), picks the device, and transcribes one job at a time in the order jobs arrive. The engine
 * stays loaded for as long as the worker lives, which is as long as a page of the workspace is open: the user's
 * device has the memory, and a reload would cost the wait. Pure of the worker scope: ``engineWorker.ts`` wires
 * ports to it, and the engine loader, the device pick and the store are injected.
 */

import { downloadPinnedFiles, ModelDownloadError, describeError } from "./download";
import type { Device, EngineSpec, EngineVariant } from "./engines";
import { engineFileUrlPrefix, engineSpec, engineVariant, pinnedFileUrl, variantFiles } from "./engines";
import type { PinnedFileStore } from "./fileStore";
import { defaultPinnedFileStore } from "./fileStore";
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
  /** Where the checked files are kept between loads; this realm's Cache Storage unless given. */
  store?: PinnedFileStore;
}

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
  const store = options.store ?? defaultPinnedFileStore();
  const ports = new Set<HostPort>();
  let loaded: LoadedEngine | null = null;
  let loading: { engineId: string; promise: Promise<LoadedEngine> } | null = null;
  // One job at a time, in arrival order across every port.
  let lane: Promise<unknown> = Promise.resolve();

  const broadcast = (message: WorkerMessage): void => {
    for (const port of ports) port.postMessage(message);
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
    const pinned = variantFiles(spec, variant);
    const files = await download(spec, pinned, {
      store,
      onProgress: (loadedBytes, totalBytes) => broadcast({ type: "progress", loadedBytes, totalBytes }),
    });
    broadcast({ type: "loading" });
    const engine = await options.loadEngine(spec, variant, files, { assetBase });
    // The store keeps one variant of the engine, the one this device runs, at the commit the registry pins.
    await store.prune(engineFileUrlPrefix(spec), new Set(pinned.map((file) => pinnedFileUrl(spec, file))));
    return engine;
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
