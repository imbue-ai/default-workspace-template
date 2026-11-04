/**
 * The messages between a page and the engine worker (the workspace-voice-input spec, D6): one worker per
 * workspace and engine, which every chat page connects to, holding the one loaded model.
 */

import type { Device, EngineId } from "./engines";
import type { TimedWord } from "./speechEngine";

export type EngineLoadState = "unloaded" | "downloading" | "loading" | "ready";

export type PageMessage =
  /** Load the engine unless it is loaded: download its files, pick the device, and say when it is ready. */
  | { type: "load"; engineId: EngineId; preferredDevice: Device | null; assetBase: string }
  /** Transcribe 16 kHz mono samples; ``audio`` is transferred, not copied. */
  | { type: "transcribe"; id: number; audio: ArrayBuffer };

export type WorkerMessage =
  | { type: "progress"; loadedBytes: number; totalBytes: number }
  /** The files are down and checked; the engine is loading, which reports no progress. */
  | { type: "loading" }
  | { type: "ready"; engineId: EngineId; device: Device }
  /** The load failed; ``filePath`` names the file when a download did. */
  | { type: "load-error"; message: string; filePath: string | null }
  /** The engine was released after sitting idle; the next load downloads it again. */
  | { type: "unloaded" }
  | { type: "result"; id: number; text: string; words: readonly TimedWord[] | null }
  | { type: "transcribe-error"; id: number; message: string };
