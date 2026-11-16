/**
 * What a loaded speech engine is to the rest of the library (the workspace-voice-input spec, D2): 16 kHz mono
 * samples in, text out, with word timings when the engine has them. Only the engine worker holds one; the page
 * reaches it through the worker protocol.
 */

export interface TimedWord {
  readonly text: string;
  readonly startSeconds: number;
  readonly endSeconds: number;
}

export interface TranscriptionResult {
  readonly text: string;
  /** The words with their times, or null for an engine that gives none. */
  readonly words: readonly TimedWord[] | null;
}

export interface SpeechEngine {
  transcribe(samples16k: Float32Array): Promise<TranscriptionResult>;
  dispose(): Promise<void>;
}

export const SAMPLE_RATE = 16000;
