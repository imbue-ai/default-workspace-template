import { QueryObserver, environmentManager, onlineManager } from "@tanstack/query-core";
import type { QueryClient } from "@tanstack/query-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NO_FILE_STORE } from "./fileStore";
import {
  SPEECH_MODEL_CHANNEL_NAME,
  createSpeechModels,
  createVoiceQueryClient,
  defaultSpeechModelsDeps,
  getSpeechModels,
  getVoiceQueryClient,
} from "./speechModel";
import type { SpeechModel, SpeechModelChannel, SpeechModels } from "./speechModel";
import type { PageDictationOptions } from "./support";
import { EngineLoadError, EngineWorkerLostError, connectEngineWorker } from "./workerClient";
import type { EngineWorkerPort } from "./workerClient";
import { createWorkerHost } from "./workerHost";
import type { PageMessage, WorkerMessage } from "./workerProtocol";

const ENGINE_ID = "moonshine-base";
const OPTIONS: PageDictationOptions = {
  engineId: ENGINE_ID,
  preferredDevice: null,
  assetBase: "https://chat.test/assets/voice/",
};
const READY: WorkerMessage = { type: "ready", engineId: ENGINE_ID, device: "wasm" };

// TanStack takes a window-less realm for a server, where it never collects a query; these tests are a page's.
environmentManager.setIsServer(() => false);

/** The page's end of a port to the engine worker, which the test speaks for. */
class FakePort implements EngineWorkerPort {
  readonly sent: PageMessage[] = [];
  onmessage: ((event: MessageEvent<WorkerMessage>) => void) | null = null;
  onmessageerror: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  postMessage(message: PageMessage): void {
    this.sent.push(message);
  }
  say(message: WorkerMessage): void {
    this.onmessage?.({ data: message } as MessageEvent<WorkerMessage>);
  }
  /** The worker fails under the page, which takes it for lost. */
  crash(): void {
    this.onerror?.({ message: "the worker crashed" } as ErrorEvent);
  }
  loadsAsked(): number {
    return this.sent.filter((message) => message.type === "load").length;
  }
}

class FakeChannel implements SpeechModelChannel {
  readonly posted: unknown[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  postMessage(message: unknown): void {
    this.posted.push(message);
  }
  close(): void {}
  /** Another page of the workspace posts ``data``. */
  hear(data: unknown): void {
    this.onmessage?.({ data } as MessageEvent);
  }
}

/** A page's speech models over the real worker client, so a load settles and a loss is told in the order the
 *  page will see them; each connection is a port the test answers as the worker. */
function harness({ isShared = true, hasChannel = true }: { isShared?: boolean; hasChannel?: boolean } = {}) {
  const queryClient = createVoiceQueryClient();
  const ports: FakePort[] = [];
  const channel = new FakeChannel();
  const createChannel = vi.fn(() => (hasChannel ? channel : null));
  const models = createSpeechModels({
    queryClient,
    connect: (engineId, assetBase) =>
      connectEngineWorker(engineId, assetBase, () => {
        const port = new FakePort();
        ports.push(port);
        return port;
      }),
    isWorkerShared: () => isShared,
    createChannel,
  });
  /** The newest connection's port. */
  const port = (): FakePort => ports[ports.length - 1];
  const loadState = () => queryClient.getQueryState<SpeechModel>(models.loadQueryOptions(OPTIONS).queryKey);
  const progress = () => queryClient.getQueryData(models.progressQueryKey(OPTIONS));
  return { queryClient, models, ports, port, channel, createChannel, loadState, progress };
}

/** A surface's view of the engine's load: an observer that fetches nothing itself, and each state it was told. */
function observe(queryClient: QueryClient, models: SpeechModels) {
  const observer = new QueryObserver<SpeechModel, Error>(queryClient, {
    ...models.loadQueryOptions(OPTIONS),
    enabled: false,
  });
  const seen: string[] = [];
  observer.subscribe((result) => seen.push(`${result.status}/${result.fetchStatus}`));
  return { observer, seen };
}

async function settled(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("the page's speech models", () => {
  it("shows an observer the load a request runs: idle, then fetching, then the model", async () => {
    const { queryClient, models, ports, port } = harness();
    const { observer, seen } = observe(queryClient, models);
    expect(observer.getCurrentResult()).toMatchObject({ status: "pending", fetchStatus: "idle" });
    // Observing loads nothing, and connects to nothing.
    expect(ports).toEqual([]);
    const requested = models.request(OPTIONS);
    expect(observer.getCurrentResult()).toMatchObject({ status: "pending", fetchStatus: "fetching" });
    expect(port().sent).toEqual([
      { type: "load", engineId: ENGINE_ID, preferredDevice: null, assetBase: OPTIONS.assetBase },
    ]);
    port().say({ type: "ready", engineId: ENGINE_ID, device: "webgpu" });
    await expect(requested).resolves.toEqual({ device: "webgpu" });
    expect(observer.getCurrentResult()).toMatchObject({
      status: "success",
      fetchStatus: "idle",
      data: { device: "webgpu" },
    });
    expect(seen[0]).toBe("pending/fetching");
    expect(seen[seen.length - 1]).toBe("success/idle");
  });

  it("runs one load however many ask, and answers a request after it from what is loaded", async () => {
    const { models, ports, port } = harness();
    const first = models.request(OPTIONS);
    const second = models.request({ ...OPTIONS });
    expect(port().loadsAsked()).toBe(1);
    port().say(READY);
    await expect(first).resolves.toEqual({ device: "wasm" });
    await expect(second).resolves.toEqual({ device: "wasm" });
    await expect(models.request(OPTIONS)).resolves.toEqual({ device: "wasm" });
    expect(ports.length).toBe(1);
    expect(port().loadsAsked()).toBe(1);
  });

  it("asks the worker for the device the page prefers, under keys of that engine's and device's own", async () => {
    const { models, port } = harness();
    const preferring: PageDictationOptions = { ...OPTIONS, preferredDevice: "wasm" };
    expect(models.loadQueryOptions(preferring).queryKey).toEqual(["speech-model", ENGINE_ID, "wasm"]);
    expect(models.progressQueryKey(preferring)).toEqual(["speech-model-progress", ENGINE_ID, "wasm"]);
    const requested = models.request(preferring);
    expect(port().sent).toEqual([
      { type: "load", engineId: ENGINE_ID, preferredDevice: "wasm", assetBase: OPTIONS.assetBase },
    ]);
    port().say(READY);
    await requested;
  });

  it("manages each engine's load apart, on a port of its own", async () => {
    const { queryClient, models, ports } = harness();
    const other: PageDictationOptions = { ...OPTIONS, engineId: "another-engine" };
    const requested = models.request(OPTIONS);
    ports[0].say(READY);
    await requested;
    expect(queryClient.getQueryState(models.loadQueryOptions(other).queryKey)).toBeUndefined();
    const requestedOther = models.request(other);
    expect(ports.length).toBe(2);
    expect(ports[1].sent).toEqual([
      { type: "load", engineId: "another-engine", preferredDevice: null, assetBase: OPTIONS.assetBase },
    ]);
    expect(models.engine(other)).not.toBe(models.engine(OPTIONS));
    ports[1].say({ type: "ready", engineId: "another-engine", device: "webgpu" });
    await expect(requestedOther).resolves.toEqual({ device: "webgpu" });
    await expect(models.request(OPTIONS)).resolves.toEqual({ device: "wasm" });
  });

  it("rejects a request with why the load failed, leaves the load in error, and loads again when asked again", async () => {
    const { models, port, loadState } = harness();
    const failed = models.request(OPTIONS).catch((error: unknown) => error);
    port().say({
      type: "load-error",
      message: "config.json does not match its pinned SHA-256",
      filePath: "config.json",
    });
    const error = await failed;
    expect(error).toBeInstanceOf(EngineLoadError);
    expect((error as EngineLoadError).filePath).toBe("config.json");
    expect(loadState()).toMatchObject({ status: "error", fetchStatus: "idle" });
    expect(loadState()?.error).toBe(error);
    const retried = models.request(OPTIONS);
    expect(port().loadsAsked()).toBe(2);
    expect(loadState()).toMatchObject({ status: "pending", fetchStatus: "fetching" });
    port().say(READY);
    await expect(retried).resolves.toEqual({ device: "wasm" });
  });

  it("keeps the progress of the load the worker is running, until the load ends or the engine is let go", () => {
    const { models, port, progress } = harness();
    models.engine(OPTIONS);
    const ends: WorkerMessage[] = [READY, { type: "load-error", message: "no", filePath: null }, { type: "unloaded" }];
    for (const end of ends) {
      port().say({ type: "progress", loadedBytes: 10, totalBytes: 100 });
      expect(progress()).toEqual({ loadedBytes: 10, totalBytes: 100 });
      port().say(end);
      expect(progress()).toBeNull();
    }
  });

  it("learns the model is loaded from a ready it never asked for", async () => {
    const { queryClient, models, port } = harness();
    const { observer } = observe(queryClient, models);
    models.engine(OPTIONS);
    port().say({ type: "ready", engineId: ENGINE_ID, device: "webgpu" });
    expect(observer.getCurrentResult()).toMatchObject({
      status: "success",
      fetchStatus: "idle",
      data: { device: "webgpu" },
    });
    await expect(models.request(OPTIONS)).resolves.toEqual({ device: "webgpu" });
    expect(port().loadsAsked()).toBe(0);
  });

  it("takes the model for not loaded once the worker lets the engine go, and loads again on the port it has", async () => {
    const { queryClient, models, ports, port } = harness();
    const { observer } = observe(queryClient, models);
    const requested = models.request(OPTIONS);
    port().say(READY);
    await requested;
    port().say({ type: "unloaded" });
    expect(observer.getCurrentResult()).toMatchObject({ status: "pending", fetchStatus: "idle" });
    expect(observer.getCurrentResult().data).toBeUndefined();
    const again = models.request(OPTIONS);
    expect(ports.length).toBe(1);
    expect(port().loadsAsked()).toBe(2);
    port().say(READY);
    await expect(again).resolves.toEqual({ device: "wasm" });
  });

  it("keeps a load in flight when the worker lets another engine go to make room for it", async () => {
    const { models, port } = harness();
    const requested = models.request(OPTIONS);
    port().say({ type: "unloaded" });
    port().say(READY);
    await expect(requested).resolves.toEqual({ device: "wasm" });
  });

  it("takes the model for not loaded when the worker is lost, and connects anew when next asked for its port", () => {
    const { queryClient, models, ports, port } = harness();
    const { observer } = observe(queryClient, models);
    const first = models.engine(OPTIONS);
    expect(models.engine(OPTIONS)).toBe(first);
    port().say(READY);
    expect(observer.getCurrentResult().status).toBe("success");
    port().crash();
    expect(observer.getCurrentResult()).toMatchObject({ status: "pending", fetchStatus: "idle" });
    expect(observer.getCurrentResult().data).toBeUndefined();
    expect(ports.length).toBe(1);
    const second = models.engine(OPTIONS);
    expect(second).not.toBe(first);
    expect(second.isDead).toBe(false);
    expect(ports.length).toBe(2);
  });

  it("rejects a request whose worker is lost mid-load with the loss, and loads on a new connection when asked again", async () => {
    const { models, ports, port, loadState, progress } = harness();
    const requested = models.request(OPTIONS).catch((error: unknown) => error);
    port().say({ type: "progress", loadedBytes: 10, totalBytes: 100 });
    port().crash();
    const error = await requested;
    expect(error).toBeInstanceOf(EngineWorkerLostError);
    expect(loadState()).toMatchObject({ status: "error", fetchStatus: "idle" });
    expect(loadState()?.error).toBe(error);
    expect(progress()).toBeNull();
    const retried = models.request(OPTIONS);
    expect(ports.length).toBe(2);
    expect(port().loadsAsked()).toBe(1);
    port().say(READY);
    await expect(retried).resolves.toEqual({ device: "wasm" });
  });

  it("takes a hello naming a loaded engine for the model being loaded, and a hello naming none for nothing", () => {
    const empty = harness();
    const { observer } = observe(empty.queryClient, empty.models);
    empty.models.engine(OPTIONS);
    empty.port().say({ type: "hello", loaded: null });
    expect(observer.getCurrentResult()).toMatchObject({ status: "pending", fetchStatus: "idle" });

    const holding = harness();
    holding.models.engine(OPTIONS);
    holding.port().say({ type: "hello", loaded: { engineId: ENGINE_ID, device: "webgpu" } });
    expect(holding.loadState()).toMatchObject({ status: "success", data: { device: "webgpu" } });
  });
});

describe("discovering what the workspace's worker holds", () => {
  it("has nothing to find out where the worker is the page's own, or the model was never downloaded", async () => {
    const dedicated = harness({ isShared: false });
    await dedicated.models.discover(OPTIONS, true);
    expect(dedicated.ports).toEqual([]);
    expect(dedicated.createChannel).not.toHaveBeenCalled();

    const neverDownloaded = harness();
    await neverDownloaded.models.discover(OPTIONS, false);
    expect(neverDownloaded.ports).toEqual([]);
  });

  it("connects to a shared worker whose model is downloaded, and knows once the worker says what it holds", async () => {
    const { models, ports, port, loadState } = harness();
    let isKnown = false;
    const discovered = models.discover(OPTIONS, true).then(() => {
      isKnown = true;
    });
    await settled();
    expect(ports.length).toBe(1);
    // Finding out asks the worker to load nothing.
    expect(port().sent).toEqual([]);
    expect(isKnown).toBe(false);
    port().say({ type: "hello", loaded: { engineId: ENGINE_ID, device: "webgpu" } });
    await discovered;
    expect(loadState()).toMatchObject({ status: "success", data: { device: "webgpu" } });
    // What the page has already heard it knows at once, on the connection it has.
    await models.discover(OPTIONS, true);
    expect(ports.length).toBe(1);
  });

  it("knows a shared worker holds nothing once it says so, or once it is lost", async () => {
    const empty = harness();
    const { observer } = observe(empty.queryClient, empty.models);
    const discovered = empty.models.discover(OPTIONS, true);
    empty.port().say({ type: "hello", loaded: null });
    await discovered;
    expect(observer.getCurrentResult()).toMatchObject({ status: "pending", fetchStatus: "idle" });

    const lost = harness();
    const unanswered = lost.models.discover(OPTIONS, true);
    lost.port().crash();
    await unanswered;
    expect(lost.loadState()?.data).toBeUndefined();
  });

  it("connects when another page asks for a model this page asked after, and so hears the worker load it", async () => {
    const { queryClient, models, ports, port, channel, progress } = harness();
    const { observer } = observe(queryClient, models);
    await models.discover(OPTIONS, false);
    expect(ports).toEqual([]);
    channel.hear({ type: "speech-model-requested", engineId: ENGINE_ID });
    expect(ports.length).toBe(1);
    expect(port().sent).toEqual([]);
    port().say({ type: "hello", loaded: null });
    port().say({ type: "progress", loadedBytes: 40, totalBytes: 100 });
    expect(progress()).toEqual({ loadedBytes: 40, totalBytes: 100 });
    expect(observer.getCurrentResult()).toMatchObject({ status: "pending", fetchStatus: "idle" });
    port().say(READY);
    expect(observer.getCurrentResult()).toMatchObject({ status: "success", data: { device: "wasm" } });
    // Connected already, the page has nothing to do when the model is asked for again.
    channel.hear({ type: "speech-model-requested", engineId: ENGINE_ID });
    expect(ports.length).toBe(1);
  });

  it("connects to nothing for a model it never asked after, or for anything else said on the channel", async () => {
    const { models, ports, channel } = harness();
    await models.discover(OPTIONS, false);
    channel.hear({ type: "speech-model-requested", engineId: "another-engine" });
    channel.hear({ type: "speech-model-requested" });
    channel.hear({ type: "dictation-started", token: "another-page" });
    channel.hear("speech-model-requested");
    channel.hear(null);
    expect(ports).toEqual([]);
  });

  it("tells the workspace's other pages a model is asked for, on one channel, only where the worker is shared", async () => {
    const shared = harness();
    await shared.models.discover(OPTIONS, false);
    const requested = shared.models.request(OPTIONS);
    expect(shared.channel.posted).toEqual([{ type: "speech-model-requested", engineId: ENGINE_ID }]);
    expect(shared.createChannel).toHaveBeenCalledTimes(1);
    shared.port().say(READY);
    await requested;

    const dedicated = harness({ isShared: false });
    const own = dedicated.models.request(OPTIONS);
    expect(dedicated.createChannel).not.toHaveBeenCalled();
    dedicated.port().say(READY);
    await expect(own).resolves.toEqual({ device: "wasm" });

    const withoutChannel = harness({ hasChannel: false });
    const quiet = withoutChannel.models.request(OPTIONS);
    withoutChannel.port().say(READY);
    await expect(quiet).resolves.toEqual({ device: "wasm" });
  });
});

/** A workspace: the one worker (the real host, its download in the test's hands), and pages that reach it over
 *  ports and each other over a channel, both delivering as the real ones do: later, and in order. */
function workspace() {
  let reportProgress: (loadedBytes: number, totalBytes: number) => void = () => undefined;
  let finishDownload: () => void = () => undefined;
  const loadsStarted = vi.fn();
  const host = createWorkerHost({
    loadEngine: async () => ({ transcribe: async () => ({ text: "", words: null }), dispose: async () => undefined }),
    pickDevice: async () => "wasm",
    download: async (_spec, files, options = {}) => {
      loadsStarted();
      reportProgress = options.onProgress ?? reportProgress;
      await new Promise<void>((resolve) => {
        finishDownload = resolve;
      });
      return new Map(files.map((file) => [file.path, new ArrayBuffer(1)]));
    },
    store: NO_FILE_STORE,
  });
  const later = (deliver: () => void): void => void Promise.resolve().then(deliver);
  const openPort = (): EngineWorkerPort => {
    const pagePort: EngineWorkerPort = {
      onmessage: null,
      postMessage: (message) => later(() => void handle(message)),
    };
    const handle = host.connect({
      postMessage: (message) => later(() => pagePort.onmessage?.({ data: message } as MessageEvent<WorkerMessage>)),
    });
    return pagePort;
  };
  const channels: FakeChannel[] = [];
  const openPage = () => {
    const queryClient = createVoiceQueryClient();
    const channel = new FakeChannel();
    channel.postMessage = (message) => {
      for (const other of channels) if (other !== channel) later(() => other.hear(message));
    };
    channels.push(channel);
    const models = createSpeechModels({
      queryClient,
      connect: (engineId, assetBase) => connectEngineWorker(engineId, assetBase, openPort),
      isWorkerShared: () => true,
      createChannel: () => channel,
    });
    return {
      models,
      ...observe(queryClient, models),
      progress: () => queryClient.getQueryData(models.progressQueryKey(OPTIONS)),
    };
  };
  return {
    openPage,
    loadsStarted,
    reportProgress: (loadedBytes: number, totalBytes: number) => reportProgress(loadedBytes, totalBytes),
    finishDownload: () => finishDownload(),
  };
}

describe("the pages of a workspace", () => {
  it("stay in step through the one worker: a load one page asks for is every page's, then and for a page opened after", async () => {
    const { openPage, loadsStarted, reportProgress, finishDownload } = workspace();
    const asking = openPage();
    const watching = openPage();
    await asking.models.discover(OPTIONS, false);
    await watching.models.discover(OPTIONS, false);

    const requested = asking.models.request(OPTIONS);
    await settled();
    reportProgress(40, 100);
    await settled();
    expect(asking.observer.getCurrentResult()).toMatchObject({ status: "pending", fetchStatus: "fetching" });
    expect(asking.progress()).toEqual({ loadedBytes: 40, totalBytes: 100 });
    // The page that did not ask is not loading, and sees the load that is running.
    expect(watching.observer.getCurrentResult()).toMatchObject({ status: "pending", fetchStatus: "idle" });
    expect(watching.progress()).toEqual({ loadedBytes: 40, totalBytes: 100 });

    // Asking too joins the load the worker is running.
    const joined = watching.models.request(OPTIONS);
    await settled();
    expect(watching.observer.getCurrentResult()).toMatchObject({ status: "pending", fetchStatus: "fetching" });
    finishDownload();
    await expect(requested).resolves.toEqual({ device: "wasm" });
    await expect(joined).resolves.toEqual({ device: "wasm" });
    await settled();
    for (const page of [asking, watching]) {
      expect(page.observer.getCurrentResult()).toMatchObject({
        status: "success",
        fetchStatus: "idle",
        data: { device: "wasm" },
      });
      expect(page.progress()).toBeNull();
    }

    // A page opened once the model is on the device finds it loaded without asking, and asking loads nothing.
    const opened = openPage();
    await opened.models.discover(OPTIONS, true);
    expect(opened.observer.getCurrentResult()).toMatchObject({ status: "success", data: { device: "wasm" } });
    await expect(opened.models.request(OPTIONS)).resolves.toEqual({ device: "wasm" });
    expect(loadsStarted).toHaveBeenCalledTimes(1);
  });
});

describe("the voice query client", () => {
  it("loads a model with the network reported down", async () => {
    onlineManager.setOnline(false);
    const { models, ports, port, loadState } = harness();
    const requested = models.request(OPTIONS);
    expect(ports.length).toBe(1);
    expect(loadState()?.fetchStatus).toBe("fetching");
    port().say(READY);
    await expect(requested).resolves.toEqual({ device: "wasm" });
  });

  it("tells a failed load once rather than asking the worker again by itself", async () => {
    const { queryClient, models, port } = harness();
    const { observer } = observe(queryClient, models);
    const refetched = observer.refetch();
    port().say({ type: "load-error", message: "no", filePath: null });
    await vi.advanceTimersByTimeAsync(60_000);
    expect((await refetched).status).toBe("error");
    expect(port().loadsAsked()).toBe(1);
  });

  it("keeps a loaded model loaded however long nothing looks at it", async () => {
    const { models, port } = harness();
    const requested = models.request(OPTIONS);
    port().say(READY);
    await requested;
    await vi.advanceTimersByTimeAsync(24 * 60 * 60 * 1000);
    const aDayOn = models.request(OPTIONS);
    expect(port().loadsAsked()).toBe(1);
    await expect(aDayOn).resolves.toEqual({ device: "wasm" });
  });
});

describe("the page's own speech models", () => {
  it("are one for the page, over the page's one query client", () => {
    expect(getVoiceQueryClient()).toBe(getVoiceQueryClient());
    expect(getSpeechModels()).toBe(getSpeechModels());
    expect(defaultSpeechModelsDeps().queryClient).toBe(getVoiceQueryClient());
  });

  it("hear the workspace on a channel of their own, where the realm has one", () => {
    const opened: string[] = [];
    vi.stubGlobal(
      "BroadcastChannel",
      class {
        constructor(name: string) {
          opened.push(name);
        }
      },
    );
    expect(defaultSpeechModelsDeps().createChannel()).not.toBeNull();
    expect(opened).toEqual([SPEECH_MODEL_CHANNEL_NAME]);
    vi.stubGlobal("BroadcastChannel", undefined);
    expect(defaultSpeechModelsDeps().createChannel()).toBeNull();
  });
});
