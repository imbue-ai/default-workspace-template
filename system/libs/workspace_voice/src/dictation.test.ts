import { beforeEach, describe, expect, it, vi } from "vitest";
import { createDictation, smoothLevel } from "./dictation";
import type { DictationChannel, DictationEvents, DictationProblem, DictationState } from "./dictation";
import type { SpeechDetector, SpeechDetectorOptions } from "./speechDetector";
import type { TranscriptionResult } from "./speechEngine";
import { EngineLoadError } from "./workerClient";
import type { EngineWorkerClient } from "./workerClient";

const FRAME_SAMPLES = 512;
const ASSET_BASE = "https://chat.test/assets/voice/";

class FakeClient implements EngineWorkerClient {
  readonly engineId = "moonshine-base";
  isDead = false;
  loadError: Error | null = null;
  readonly jobs: { audio: Float32Array; resolve(text: string): void; reject(error: Error): void }[] = [];
  load = vi.fn(async (_device: unknown, onProgress: (l: number, t: number) => void): Promise<"wasm"> => {
    if (this.loadError !== null) throw this.loadError;
    onProgress(50, 100);
    onProgress(100, 100);
    return "wasm";
  });
  transcribe(audio: Float32Array): Promise<TranscriptionResult> {
    return new Promise((resolve, reject) => {
      this.jobs.push({ audio, resolve: (text) => resolve({ text, words: null }), reject });
    });
  }
  onUnloaded(): void {}
  dispose(): void {
    this.isDead = true;
  }
}

class FakeDetector implements SpeechDetector {
  static startError: Error | null = null;
  readonly start = vi.fn(async () => {
    if (FakeDetector.startError !== null) throw FakeDetector.startError;
  });
  readonly release = vi.fn(async () => undefined);
  constructor(readonly options: SpeechDetectorOptions) {}
  frame(level: number): void {
    const frame = new Float32Array(FRAME_SAMPLES);
    frame.fill(level);
    this.options.onFrame(frame, level > 0.05 ? 0.9 : 0.1);
  }
}

class FakeChannel implements DictationChannel {
  readonly posted: unknown[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  postMessage(message: unknown): void {
    this.posted.push(message);
  }
  close(): void {}
  hear(data: unknown): void {
    this.onmessage?.({ data } as MessageEvent);
  }
}

interface Recorder extends DictationEvents {
  states: DictationState[];
  progress: [number, number][];
  levels: number[];
  finals: string[];
  tentatives: string[];
  utteranceEnds: number;
  problems: (DictationProblem | null)[];
}

function recorder(): Recorder {
  const record: Recorder = {
    states: [],
    progress: [],
    levels: [],
    finals: [],
    tentatives: [],
    utteranceEnds: 0,
    problems: [],
    onStateChange: (state) => void record.states.push(state),
    onProgress: (loaded, total) => void record.progress.push([loaded, total]),
    onLevel: (level) => void record.levels.push(level),
    onFinalWords: (words) => void record.finals.push(words),
    onTentativeWords: (words) => void record.tentatives.push(words),
    onUtteranceEnd: () => {
      record.utteranceEnds += 1;
    },
    onProblem: (problem) => void record.problems.push(problem),
  };
  return record;
}

async function settled(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}

function harness() {
  const client = new FakeClient();
  const channel = new FakeChannel();
  const events = recorder();
  const detectors: FakeDetector[] = [];
  let now = 1000;
  const clock = { advance: (ms: number) => (now += ms) };
  const dictation = createDictation(
    { engineId: "moonshine-base", assetBase: ASSET_BASE, preferredDevice: null },
    events,
    {
      connect: () => client,
      createDetector: async (options) => {
        const detector = new FakeDetector(options);
        detectors.push(detector);
        return detector;
      },
      getStream: async () => ({}) as MediaStream,
      isMicrophoneAllowedByFrame: () => true,
      now: () => now,
      createChannel: () => channel,
    },
  );
  const detector = (): FakeDetector => detectors[detectors.length - 1];
  /** Feed ``count`` frames of ``level`` 32 ms apart. */
  const speak = (count: number, level = 0.1): void => {
    for (let i = 0; i < count; i += 1) {
      clock.advance(32);
      detector().frame(level);
    }
  };
  return { client, channel, events, dictation, detector, detectors, speak, clock };
}

beforeEach(() => {
  FakeDetector.startError = null;
});

describe("dictation", () => {
  it("loads the engine, opens the microphone, and listens, announcing itself to the workspace", async () => {
    const { client, channel, events, dictation, detector } = harness();
    await dictation.start();
    expect(events.states).toEqual(["loading", "starting", "listening"]);
    expect(events.progress).toEqual([
      [50, 100],
      [100, 100],
    ]);
    expect(client.load).toHaveBeenCalledWith(null, expect.any(Function));
    expect(detector().start).toHaveBeenCalledTimes(1);
    expect(detector().options.assetBase).toBe(ASSET_BASE);
    expect(channel.posted).toEqual([{ type: "dictation-started", token: expect.any(String) }]);
    expect(events.problems).toEqual([null]);
  });

  it("previews the utterance's words as a tail, then lands them as final words when it ends", async () => {
    const { client, events, dictation, detector, speak } = harness();
    await dictation.start();
    // Frames before speech are kept as the pre-roll the turn opens with.
    speak(12, 0.001);
    detector().options.onSpeechStart();
    speak(10);
    expect(client.jobs.length).toBe(0);
    speak(8);
    expect(client.jobs.length).toBe(1);
    // The first preview went out once 500 ms of the turn had passed (its sixteenth frame), carrying the
    // pre-roll's last ten frames ahead of the spoken ones.
    expect(client.jobs[0].audio.length).toBe(26 * FRAME_SAMPLES);
    client.jobs[0].resolve("hello there");
    await settled();
    expect(events.tentatives).toEqual(["hello there"]);
    expect(events.finals).toEqual([]);
    detector().options.onSpeechEnd();
    await settled();
    expect(client.jobs.length).toBe(2);
    client.jobs[1].resolve("Hello there, world.");
    await settled();
    expect(events.tentatives).toEqual(["hello there", ""]);
    expect(events.finals).toEqual(["Hello there, world."]);
    expect(events.utteranceEnds).toBe(1);
    expect(events.levels.length).toBeGreaterThan(0);
    expect(Math.max(...events.levels)).toBeLessThanOrEqual(1);
  });

  it("keeps an earlier utterance's late words ahead of the next utterance's tail", async () => {
    const { client, events, dictation, detector, speak } = harness();
    await dictation.start();
    detector().options.onSpeechStart();
    speak(20);
    detector().options.onSpeechEnd();
    await settled();
    // The first utterance's preview is still running when the second begins and asks for its own.
    detector().options.onSpeechStart();
    speak(20);
    expect(client.jobs.length).toBe(1);
    client.jobs[0].resolve("one two");
    await settled();
    // A preview of an utterance that has ended shows nothing; its drain runs next, then the newer preview.
    expect(client.jobs.length).toBe(2);
    client.jobs[1].resolve("First words.");
    await settled();
    expect(client.jobs.length).toBe(3);
    client.jobs[2].resolve("second");
    await settled();
    expect(events.finals).toEqual(["First words."]);
    expect(events.tentatives).toEqual(["second"]);
    expect(events.utteranceEnds).toBe(0);
  });

  it("stops: releases the microphone at once, finishes the open utterance, then idles", async () => {
    const { client, events, dictation, detector, speak } = harness();
    await dictation.start();
    detector().options.onSpeechStart();
    speak(5);
    const stopping = dictation.stop();
    await settled();
    expect(detector().release).toHaveBeenCalledTimes(1);
    expect(dictation.state).toBe("stopping");
    client.jobs[0].resolve("Done.");
    await stopping;
    expect(events.finals).toEqual(["Done."]);
    expect(events.states).toEqual(["loading", "starting", "listening", "stopping", "idle"]);
    expect(events.utteranceEnds).toBe(1);
  });

  it("cancels: drops the words that are not final and keeps the microphone off", async () => {
    const { client, events, dictation, detector, speak } = harness();
    await dictation.start();
    detector().options.onSpeechStart();
    speak(20);
    client.jobs[0].resolve("unfinished words");
    await settled();
    expect(events.tentatives).toEqual(["unfinished words"]);
    detector().options.onSpeechEnd();
    await dictation.cancel();
    expect(detector().release).toHaveBeenCalledTimes(1);
    client.jobs[1]?.resolve("too late");
    await settled();
    expect(events.finals).toEqual([]);
    expect(events.tentatives[events.tentatives.length - 1]).toBe("");
    expect(dictation.state).toBe("idle");
  });

  it("tells a blocked microphone apart and idles, releasing what it opened", async () => {
    const { events, dictation, detector } = harness();
    FakeDetector.startError = Object.assign(new Error("denied"), { name: "NotAllowedError" });
    await dictation.start();
    expect(events.states).toEqual(["loading", "starting", "idle"]);
    expect(events.problems[1]).toMatchObject({ kind: "blocked", canRetry: false });
    expect(detector().release).toHaveBeenCalledTimes(1);
  });

  it("names the file when the model could not be downloaded", async () => {
    const { client, events, dictation } = harness();
    client.loadError = new EngineLoadError("config.json does not match its pinned SHA-256", "config.json");
    await dictation.start();
    expect(events.states).toEqual(["loading", "idle"]);
    expect(events.problems[1]).toMatchObject({ kind: "download", message: expect.stringContaining("config.json") });
  });

  it("holds words that keep failing, stops listening, and lands them on Retry", async () => {
    const { client, events, dictation, detector, speak } = harness();
    await dictation.start();
    detector().options.onSpeechStart();
    speak(5);
    detector().options.onSpeechEnd();
    await settled();
    for (let attempt = 0; attempt < 3; attempt += 1) {
      client.jobs[attempt].reject(new Error("engine down"));
      await settled();
    }
    expect(dictation.isHeld).toBe(true);
    expect(dictation.state).toBe("idle");
    expect(events.problems[events.problems.length - 1]).toMatchObject({ kind: "held", canRetry: true });
    expect(detector().release).toHaveBeenCalledTimes(1);
    dictation.retryHeld();
    await settled();
    client.jobs[3].resolve("Recovered words.");
    await settled();
    expect(events.finals).toEqual(["Recovered words."]);
    expect(events.problems[events.problems.length - 1]).toBeNull();
    expect(dictation.isHeld).toBe(false);
  });

  it("drops held words on Discard", async () => {
    const { client, events, dictation, detector, speak } = harness();
    await dictation.start();
    detector().options.onSpeechStart();
    speak(5);
    detector().options.onSpeechEnd();
    await settled();
    for (let attempt = 0; attempt < 3; attempt += 1) {
      client.jobs[attempt].reject(new Error("engine down"));
      await settled();
    }
    dictation.discardHeld();
    await settled();
    expect(events.finals).toEqual([]);
    expect(dictation.isHeld).toBe(false);
    expect(events.utteranceEnds).toBe(1);
  });

  it("yields to a dictation started elsewhere in the workspace", async () => {
    const { channel, events, dictation, detector } = harness();
    await dictation.start();
    channel.hear({ type: "dictation-started", token: "another-page" });
    await settled();
    expect(detector().release).toHaveBeenCalledTimes(1);
    expect(events.states[events.states.length - 1]).toBe("idle");
    // Its own announcement changes nothing.
    const { posted } = channel;
    const own = posted[0] as { token: string };
    await dictation.start();
    channel.hear({ type: "dictation-started", token: own.token });
    await settled();
    expect(dictation.state).toBe("listening");
  });

  it("meters the level with a fast attack and a slow release", () => {
    const up = smoothLevel(0, 1, 32);
    const down = smoothLevel(1, 0, 32);
    expect(up).toBeGreaterThan(0.7);
    expect(1 - down).toBeLessThan(0.3);
  });
});
