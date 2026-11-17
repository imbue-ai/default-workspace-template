import { describe, expect, it, vi } from "vitest";
import type { MicVAD, RealTimeVADOptions } from "@ricky0123/vad-web";
import { SPEECH_DETECTOR_TUNING, createSpeechDetector } from "./speechDetector";

class FakeAudioContext {
  state: AudioContextState = "running";
  resume = vi.fn(async () => {
    this.state = "running";
  });
  close = vi.fn(async () => {
    this.state = "closed";
  });
}

function fakeDeps(options: { vadError?: Error; contextState?: AudioContextState } = {}) {
  const contexts: FakeAudioContext[] = [];
  const vad = { start: vi.fn(async () => undefined), destroy: vi.fn(async () => undefined) };
  let captured: Partial<RealTimeVADOptions> | null = null;
  const deps = {
    createVad: vi.fn(async (vadOptions: Partial<RealTimeVADOptions>) => {
      captured = vadOptions;
      if (options.vadError !== undefined) throw options.vadError;
      return vad as unknown as MicVAD;
    }),
    createAudioContext: () => {
      const context = new FakeAudioContext();
      context.state = options.contextState ?? "running";
      contexts.push(context);
      return context as unknown as AudioContext;
    },
  };
  return { deps, vad, contexts, captured: () => captured! };
}

const CALLBACKS = {
  assetBase: "https://chat.test/assets/voice/",
  getStream: vi.fn(async () => ({}) as MediaStream),
  onFrame: vi.fn(),
  onSpeechStart: vi.fn(),
  onSpeechEnd: vi.fn(),
  onMisfire: vi.fn(),
};

describe("createSpeechDetector", () => {
  it("loads Silero v5 with the tuning, from the page's own files, on a context it owns, not starting on load", async () => {
    const { deps, contexts, captured } = fakeDeps();
    await createSpeechDetector(CALLBACKS, deps);
    const options = captured();
    expect(options).toMatchObject({
      ...SPEECH_DETECTOR_TUNING,
      model: "v5",
      baseAssetPath: "https://chat.test/assets/voice/vad/",
      onnxWASMBasePath: "https://chat.test/assets/voice/vad-ort/",
      startOnLoad: false,
      processorType: "AudioWorklet",
    });
    expect(options.audioContext).toBe(contexts[0]);
    expect(options.getStream).toBe(CALLBACKS.getStream);
    const ort = { env: { wasm: {} as { numThreads?: number } } };
    options.ortConfig!(ort as never);
    expect(ort.env.wasm.numThreads).toBe(1);
  });

  it("passes every frame with its speech probability, and the utterance marks, through", async () => {
    const { deps, captured } = fakeDeps();
    await createSpeechDetector(CALLBACKS, deps);
    const frame = new Float32Array(512);
    captured().onFrameProcessed!({ isSpeech: 0.9, notSpeech: 0.1 }, frame);
    expect(CALLBACKS.onFrame).toHaveBeenCalledWith(frame, 0.9);
    captured().onSpeechStart!();
    captured().onSpeechEnd!(new Float32Array(16));
    captured().onVADMisfire!();
    expect(CALLBACKS.onSpeechStart).toHaveBeenCalledTimes(1);
    expect(CALLBACKS.onSpeechEnd).toHaveBeenCalledWith();
    expect(CALLBACKS.onMisfire).toHaveBeenCalledTimes(1);
  });

  it("starts the detector and wakes a context the autoplay policy left suspended", async () => {
    const { deps, vad, contexts } = fakeDeps({ contextState: "suspended" });
    const detector = await createSpeechDetector(CALLBACKS, deps);
    await detector.start();
    expect(vad.start).toHaveBeenCalledTimes(1);
    expect(contexts[0].resume).toHaveBeenCalledTimes(1);
  });

  it("lets the microphone's own error through from start", async () => {
    const { deps, vad } = fakeDeps();
    vad.start.mockRejectedValueOnce(Object.assign(new Error("denied"), { name: "NotAllowedError" }));
    const detector = await createSpeechDetector(CALLBACKS, deps);
    await expect(detector.start()).rejects.toMatchObject({ name: "NotAllowedError" });
  });

  it("destroys the detector and closes the context on release, and closes the context when loading fails", async () => {
    const { deps, vad, contexts } = fakeDeps();
    const detector = await createSpeechDetector(CALLBACKS, deps);
    await detector.release();
    expect(vad.destroy).toHaveBeenCalledTimes(1);
    expect(contexts[0].close).toHaveBeenCalledTimes(1);

    const failing = fakeDeps({ vadError: new Error("model missing") });
    await expect(createSpeechDetector(CALLBACKS, failing.deps)).rejects.toThrow("model missing");
    expect(failing.contexts[0].close).toHaveBeenCalledTimes(1);
  });
});
