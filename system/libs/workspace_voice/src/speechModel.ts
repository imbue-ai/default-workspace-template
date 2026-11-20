/**
 * The page's speech models: one managed load per engine, held in a TanStack query cache, so every surface of the
 * page asks for a model in one place and draws the load's state from one place. The worker that holds the model
 * (``workerHost.ts``) is the authority: what it tells the page's port is mirrored into the cache, so a page
 * learns a model came ready whichever page asked for it. The workspace's pages stay in step by each hearing the
 * one worker; all they say to one another is that a model was asked for, so a page not yet connected connects.
 *
 * A load is a query a surface observes without fetching (a ``QueryObserver`` given ``enabled: false``) and asks
 * for with ``request``; its progress sits under a key of its own. Nothing here cancels a load or knows of any
 * interface: a surface that stops caring stops listening.
 */

import { QueryClient } from "@tanstack/query-core";
import type { Device, EngineId } from "./engines";
import type { PageDictationOptions } from "./support";
import type { EngineWorkerClient } from "./workerClient";
import { connectEngineWorker, isEngineWorkerShared, openEngineWorkerPort } from "./workerClient";

/** A loaded speech model. */
export interface SpeechModel {
  readonly device: Device;
}

/** How far a model's download has got. */
export interface SpeechModelProgress {
  readonly loadedBytes: number;
  readonly totalBytes: number;
}

export function createVoiceQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // A model kept on the device loads with the network down, and a download that cannot start should fail
        // as itself rather than wait, paused, for a connection.
        networkMode: "always",
        // A load runs when a surface asks for it and at no other time: a failure is told as it is, and neither
        // the window being looked at again nor the network coming back asks again.
        retry: false,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        // A loaded model stays loaded until the worker says otherwise.
        staleTime: Infinity,
        gcTime: Infinity,
      },
    },
  });
}

let voiceQueryClient: QueryClient | null = null;

/** The page's one query client, made on first use. A test makes its own with ``createVoiceQueryClient``, so no
 *  case reads another's models. */
export function getVoiceQueryClient(): QueryClient {
  voiceQueryClient ??= createVoiceQueryClient();
  return voiceQueryClient;
}

/** What every page of the workspace hears a model being asked for on. */
export interface SpeechModelChannel {
  postMessage(message: unknown): void;
  onmessage: ((event: MessageEvent) => void) | null;
  close(): void;
}

export interface SpeechModelsDeps {
  queryClient: QueryClient;
  connect(engineId: EngineId, assetBase: string): EngineWorkerClient;
  /** Whether every page of the workspace reaches the one worker, rather than each a worker of its own. */
  isWorkerShared(): boolean;
  /** What every page of the workspace hears a model being asked for on; null where the realm has none. */
  createChannel(): SpeechModelChannel | null;
}

export const SPEECH_MODEL_CHANNEL_NAME = "workspace-voice:speech-model";

export function defaultSpeechModelsDeps(): SpeechModelsDeps {
  return {
    queryClient: getVoiceQueryClient(),
    connect: (engineId, assetBase) => connectEngineWorker(engineId, assetBase, openEngineWorkerPort),
    isWorkerShared: isEngineWorkerShared,
    createChannel: () =>
      typeof BroadcastChannel === "function" ? new BroadcastChannel(SPEECH_MODEL_CHANNEL_NAME) : null,
  };
}

export interface SpeechModels {
  /** The engine's managed load as TanStack query options (key and queryFn). Observe it with a ``QueryObserver``
   *  given ``enabled: false``; ask for the load with ``request``. ``status: "success"`` means the model is
   *  loaded; ``fetchStatus: "fetching"`` means a load this page asked for is in flight. */
  loadQueryOptions(options: PageDictationOptions): {
    queryKey: readonly unknown[];
    queryFn: () => Promise<SpeechModel>;
  };
  /** Where the progress of the load the worker is running is kept, whichever page asked: null between loads,
   *  and nothing before the first load the page hears of. */
  progressQueryKey(options: PageDictationOptions): readonly unknown[];
  /** Ask for the model: one load per engine however many ask, and a load already in flight is joined rather
   *  than restarted. Settles with the model, or rejects with why it could not be loaded. Nothing cancels a
   *  load: a caller that stops caring just stops listening. */
  request(options: PageDictationOptions): Promise<SpeechModel>;
  /** Find out, without loading anything, what the workspace's worker already holds; resolves once the page
   *  knows. ``isDownloaded`` is the download probe's answer for this device. */
  discover(options: PageDictationOptions, isDownloaded: boolean): Promise<void>;
  /** The page's port to the engine's worker, which dictation transcribes on; connected on first use, and
   *  connected again after a loss. */
  engine(options: PageDictationOptions): EngineWorkerClient;
}

function loadQueryKey(options: PageDictationOptions): readonly unknown[] {
  return ["speech-model", options.engineId, options.preferredDevice];
}

function progressQueryKey(options: PageDictationOptions): readonly unknown[] {
  return ["speech-model-progress", options.engineId, options.preferredDevice];
}

interface EngineConnection {
  readonly client: EngineWorkerClient;
  /** Settles once the worker has said what it holds, or is lost before it could. */
  readonly greeted: Promise<void>;
}

export function createSpeechModels(deps: SpeechModelsDeps): SpeechModels {
  const { queryClient } = deps;
  const connections = new Map<EngineId, EngineConnection>();
  // The engines this page wants to know about, each with the options to connect by.
  const watched = new Map<EngineId, PageDictationOptions>();
  let channel: SpeechModelChannel | null = null;

  /** Keep the cache saying what the engine's worker says on ``client``. */
  const mirror = (client: EngineWorkerClient, options: PageDictationOptions): void => {
    const loadKey = loadQueryKey(options);
    const progressKey = progressQueryKey(options);
    const setProgress = (progress: SpeechModelProgress | null): void => {
      queryClient.setQueryData<SpeechModelProgress | null>(progressKey, progress);
    };
    const setLoaded = (device: Device): void => {
      queryClient.setQueryData<SpeechModel>(loadKey, { device });
      setProgress(null);
    };
    const setNotLoaded = (): void => {
      // A load this page asked for is left to settle as itself: a reset would cancel it, and its request would
      // reject as cancelled rather than with the model or with why there is none.
      void queryClient.resetQueries({ queryKey: loadKey, exact: true, fetchStatus: "idle" });
      setProgress(null);
    };
    client.onMessage((message) => {
      switch (message.type) {
        case "hello":
          if (message.loaded !== null) setLoaded(message.loaded.device);
          break;
        case "ready":
          setLoaded(message.device);
          break;
        case "progress":
          setProgress({ loadedBytes: message.loadedBytes, totalBytes: message.totalBytes });
          break;
        case "load-error":
          setProgress(null);
          break;
        case "unloaded":
          setNotLoaded();
          break;
      }
    });
    client.onLost(setNotLoaded);
  };

  const greeting = (client: EngineWorkerClient): Promise<void> =>
    new Promise((resolve) => {
      client.onMessage((message) => {
        if (message.type === "hello") resolve();
      });
      client.onLost(resolve);
    });

  const connection = (options: PageDictationOptions): EngineConnection => {
    const held = connections.get(options.engineId);
    if (held !== undefined && !held.client.isDead) return held;
    const client = deps.connect(options.engineId, options.assetBase);
    mirror(client, options);
    const made: EngineConnection = { client, greeted: greeting(client) };
    connections.set(options.engineId, made);
    return made;
  };

  /** The page's channel, opened on first use and heard from then on; null where the realm has none. */
  const openChannel = (): SpeechModelChannel | null => {
    if (channel !== null) return channel;
    channel = deps.createChannel();
    if (channel === null) return null;
    channel.onmessage = (event: MessageEvent) => {
      const data: unknown = event.data;
      if (typeof data !== "object" || data === null) return;
      const { type, engineId } = data as { type?: unknown; engineId?: unknown };
      if (type !== "speech-model-requested" || typeof engineId !== "string") return;
      // The worker is about to load a model another page asked for: connected, this page hears it happen.
      const options = watched.get(engineId);
      if (options !== undefined) connection(options);
    };
    return channel;
  };

  const loadQueryOptions = (options: PageDictationOptions) => ({
    queryKey: loadQueryKey(options),
    // The download's progress reaches the cache as the worker tells it (``mirror``), whichever page asked.
    queryFn: (): Promise<SpeechModel> =>
      connection(options)
        .client.load(options.preferredDevice, () => undefined)
        .then((device) => ({ device })),
  });

  return {
    loadQueryOptions,
    progressQueryKey,
    request(options) {
      if (deps.isWorkerShared()) {
        openChannel()?.postMessage({ type: "speech-model-requested", engineId: options.engineId });
      }
      return queryClient.query(loadQueryOptions(options));
    },
    async discover(options, isDownloaded) {
      // A worker of the page's own starts empty.
      if (!deps.isWorkerShared()) return;
      watched.set(options.engineId, options);
      openChannel();
      // Nothing can be loaded that was never downloaded, and a page whose user has not set dictation up should
      // not start the worker.
      if (isDownloaded) await connection(options).greeted;
    },
    engine: (options) => connection(options).client,
  };
}

let speechModels: SpeechModels | null = null;

/** The page's speech models, over the page's query client; made on first use. */
export function getSpeechModels(): SpeechModels {
  speechModels ??= createSpeechModels(defaultSpeechModelsDeps());
  return speechModels;
}
