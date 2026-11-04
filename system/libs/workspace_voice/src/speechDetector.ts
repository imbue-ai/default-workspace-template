/**
 * The speech detector (the workspace-voice-input spec, D3): Silero VAD v5 through vad-web, with Sculptor's tuning,
 * scoring every 32 ms frame the microphone's worklet posts and marking where utterances start and end. The
 * detector owns the microphone and the audio context, and releasing it lets both go, so the system's microphone
 * light goes out the moment dictation stops.
 */

import type { MicVAD, RealTimeVADOptions } from "@ricky0123/vad-web";

/** Local-Vocal's endpointing, as Sculptor ships it: speech at probability 0.5, the end counted below 0.35, 800 ms
 *  of such frames ends the utterance, and a segment under 200 ms is a misfire. */
export const SPEECH_DETECTOR_TUNING = {
  positiveSpeechThreshold: 0.5,
  negativeSpeechThreshold: 0.35,
  redemptionMs: 800,
  preSpeechPadMs: 300,
  minSpeechMs: 200,
} as const;

/** One frame of the detector: 512 samples, 32 ms at 16 kHz. */
export const FRAME_SAMPLES = 512;
export const FRAME_MS = 32;

export interface SpeechDetectorOptions {
  /** Where the page serves the detector's code and model: ``vad/`` and ``vad-ort/`` under it. */
  assetBase: string;
  getStream(): Promise<MediaStream>;
  /** Every frame, speech or not, with the detector's probability that it is speech. The frame's buffer is the
   *  detector's own and reused; copy it to keep it. */
  onFrame(frame: Float32Array, speechProbability: number): void;
  onSpeechStart(): void;
  onSpeechEnd(): void;
  /** Speech started but ended before ``minSpeechMs``: there was nothing to hear. */
  onMisfire(): void;
}

export interface SpeechDetector {
  /** Open the microphone and start scoring; rejects with the microphone's own error when it cannot be opened. */
  start(): Promise<void>;
  /** Release the microphone and the audio context; the detector is spent. */
  release(): Promise<void>;
}

export interface SpeechDetectorDeps {
  createVad(options: Partial<RealTimeVADOptions>): Promise<MicVAD>;
  createAudioContext(): AudioContext;
}

async function createVadFromLibrary(options: Partial<RealTimeVADOptions>): Promise<MicVAD> {
  // Imported when dictation starts, so the detector and its runtime stay out of the page's first load.
  const { MicVAD } = await import("@ricky0123/vad-web");
  return MicVAD.new(options);
}

export function defaultSpeechDetectorDeps(): SpeechDetectorDeps {
  return { createVad: createVadFromLibrary, createAudioContext: () => new AudioContext() };
}

/** Load the detector's model; the microphone is opened by ``start``. */
export async function createSpeechDetector(
  options: SpeechDetectorOptions,
  deps: SpeechDetectorDeps = defaultSpeechDetectorDeps(),
): Promise<SpeechDetector> {
  const audioContext = deps.createAudioContext();
  const stopTracks = async (stream: MediaStream): Promise<void> => {
    for (const track of stream.getTracks()) track.stop();
  };
  let vad: MicVAD;
  try {
    vad = await deps.createVad({
      ...SPEECH_DETECTOR_TUNING,
      model: "v5",
      baseAssetPath: `${options.assetBase}vad/`,
      onnxWASMBasePath: `${options.assetBase}vad-ort/`,
      audioContext,
      startOnLoad: false,
      processorType: "AudioWorklet",
      getStream: options.getStream,
      pauseStream: stopTracks,
      resumeStream: options.getStream,
      ortConfig: (ort) => {
        // Nothing isolates the page cross-origin, so there is no SharedArrayBuffer for a second thread.
        ort.env.wasm.numThreads = 1;
      },
      onFrameProcessed: (probabilities, frame) => options.onFrame(frame, probabilities.isSpeech),
      onSpeechStart: options.onSpeechStart,
      onSpeechEnd: () => options.onSpeechEnd(),
      onVADMisfire: options.onMisfire,
    });
  } catch (error) {
    await audioContext.close().catch(() => undefined);
    throw error;
  }
  return {
    async start() {
      await vad.start();
      // A click's activation can expire during a long start, leaving the context suspended under the autoplay
      // policy, and a suspended detector hears nothing.
      if (audioContext.state === "suspended") await audioContext.resume();
    },
    async release() {
      // vad-web's pause keeps the stream (and the system's microphone light) alive; destroying the detector is
      // what lets the microphone go, and it never closes a context it was handed.
      await vad.destroy().catch(() => undefined);
      await audioContext.close().catch(() => undefined);
    },
  };
}
