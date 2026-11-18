/**
 * One composer's dictation (the workspace-voice-input spec, D3 to D5 and D8): its states from the press to the
 * last word, the detector that hears utterances, and the streaming turn and the one inference lane that turn
 * words out of them. It tells the composer what it hears as final words (which the composer saves) and an
 * unfinished tail (which it only shows).
 *
 * The speech model is the page's (``speechModel.ts``), not the dictation's: a dictation needs it loaded, loads
 * nothing itself, and transcribes on the page's port to the engine worker.
 *
 * The words of a turn land in order: head slices and drained chunks are committed one after another, and a turn
 * whose words are still landing when the next begins keeps appending its final words ahead of the newer turn's
 * tail. One dictation runs per workspace: a start anywhere stops the dictation running anywhere else.
 */

import type { Device, EngineId } from "./engines";
import { engineSpec, engineWindowSeconds } from "./engines";
import { MICROPHONE_CONSTRAINTS, describeMicrophoneError, isMicrophoneAllowedByFrame } from "./microphone";
import type { MicrophoneProblem } from "./microphone";
import { CommitDiscardedError, TranscriptionScheduler } from "./scheduler";
import type { SpeechDetector, SpeechDetectorOptions } from "./speechDetector";
import { createSpeechDetector, FRAME_MS } from "./speechDetector";
import { getSpeechModels } from "./speechModel";
import { StreamingTurn, frameRms } from "./streamingTurn";
import type { EngineWorkerClient } from "./workerClient";
import { EngineLoadError, EngineWorkerLostError } from "./workerClient";

export type DictationState = "idle" | "starting" | "listening" | "stopping";

export type DictationProblemKind = MicrophoneProblem["kind"] | "download" | "engine" | "lost" | "held";

export interface DictationProblem {
  readonly kind: DictationProblemKind;
  readonly message: string;
  /** Whether a Retry can help: for held words it runs them again; otherwise it asks again for what failed, the
   *  speech model's load or the dictation's start. */
  readonly canRetry: boolean;
}

export interface DictationEvents {
  onStateChange(state: DictationState): void;
  /** The input level, 0 to 1, smoothed, every frame while listening. */
  onLevel(level: number): void;
  /** Words that are final, to add after the final words so far and save. */
  onFinalWords(words: string): void;
  /** The unfinished tail, replacing the last one; "" when there is none. */
  onTentativeWords(words: string): void;
  /** The current utterance is over and its words are all final. */
  onUtteranceEnd(): void;
  /** Something to tell the user, or null when it is settled. */
  onProblem(problem: DictationProblem | null): void;
}

export interface DictationOptions {
  engineId: EngineId;
  /** Where the page serves the voice runtime's files, as an absolute URL ending in a slash. */
  assetBase: string;
  /** A device to insist on (a touch device's WebAssembly, or an evaluation's override); null lets the worker
   *  pick. */
  preferredDevice: Device | null;
}

/** What every page of the workspace hears a dictation start on. */
export interface DictationChannel {
  postMessage(message: unknown): void;
  onmessage: ((event: MessageEvent) => void) | null;
  close(): void;
}

export interface DictationDeps {
  /** The page's port to the engine's worker, asked for at every job: the page connects again after a loss. */
  connect(options: DictationOptions): EngineWorkerClient;
  createDetector(options: SpeechDetectorOptions): Promise<SpeechDetector>;
  getStream(): Promise<MediaStream>;
  isMicrophoneAllowedByFrame(): boolean | null;
  now(): number;
  createChannel(): DictationChannel | null;
}

export interface Dictation {
  readonly state: DictationState;
  /** Whether words are held after failing to transcribe, waiting for Retry or Discard. */
  readonly isHeld: boolean;
  /** From idle: open the microphone and listen. Settles when listening, or when it could not. The speech model
   *  must be loaded already: the composer asks the page's speech models (``speechModel.ts``) for it first. */
  start(): Promise<void>;
  /** Release the microphone, finish the words already heard, then idle. */
  stop(): Promise<void>;
  /** Release the microphone and drop every word that is not final; the final words stay. */
  cancel(): Promise<void>;
  retryHeld(): void;
  discardHeld(): void;
  /** The surface is going away: release the microphone and drop what is unfinished, silently. The port to the
   *  engine's worker is the page's, and stays. */
  dispose(): void;
}

export const DICTATION_CHANNEL_NAME = "workspace-voice:dictation";

// How often the live tail refreshes (each refresh transcribes the window), once the window holds 0.3 s.
const PREVIEW_INTERVAL_MS = 500;
const PREVIEW_MIN_SAMPLES = 4800;
// The detector's pre-speech pad, kept as frames ahead of speech so a soft onset is not clipped.
const PRE_ROLL_FRAMES = 10;
// The mock's meter: RMS times four, with a fast attack and a slow release.
const LEVEL_GAIN = 4;
const LEVEL_ATTACK_MS = 25;
const LEVEL_RELEASE_MS = 140;

const HELD_WORDS_PROBLEM: DictationProblem = {
  kind: "held",
  message: "Some of your words couldn't be turned into text. Retry them, or discard them and keep what's in the box.",
  canRetry: true,
};

export function smoothLevel(shown: number, target: number, dtMs: number): number {
  const timeConstant = target > shown ? LEVEL_ATTACK_MS : LEVEL_RELEASE_MS;
  return shown + (target - shown) * (1 - Math.exp(-dtMs / timeConstant));
}

export function defaultDictationDeps(): DictationDeps {
  return {
    connect: (options) => getSpeechModels().engine(options),
    createDetector: (options) => createSpeechDetector(options),
    getStream: () => navigator.mediaDevices.getUserMedia(MICROPHONE_CONSTRAINTS),
    isMicrophoneAllowedByFrame: () => isMicrophoneAllowedByFrame(document),
    now: () => performance.now(),
    createChannel: () =>
      typeof BroadcastChannel === "function" ? new BroadcastChannel(DICTATION_CHANNEL_NAME) : null,
  };
}

/** A failed load of the speech model as a problem to tell the user; ``error`` is what a request for the model
 *  (``SpeechModels.request``) rejected with. */
export function describeSpeechModelLoadError(error: unknown): DictationProblem {
  if (error instanceof EngineLoadError && error.filePath !== null) {
    return {
      kind: "download",
      message: `The speech model couldn't be downloaded (${error.filePath}). Check your internet connection and try again.`,
      canRetry: true,
    };
  }
  if (error instanceof EngineWorkerLostError) {
    return { kind: "lost", message: "The speech engine stopped responding. Try again.", canRetry: true };
  }
  const detail = error instanceof Error ? error.message : String(error);
  return { kind: "engine", message: `The speech model couldn't be loaded: ${detail}`, canRetry: true };
}

/** Whether ``problem`` is a failed load of the speech model, as ``describeSpeechModelLoadError`` tells one,
 *  rather than a problem with the microphone or with held words. */
export function isSpeechModelLoadProblem(problem: DictationProblem): boolean {
  return problem.kind === "download" || problem.kind === "engine" || problem.kind === "lost";
}

export function createDictation(
  options: DictationOptions,
  events: DictationEvents,
  deps: DictationDeps = defaultDictationDeps(),
): Dictation {
  const spec = engineSpec(options.engineId);
  const windowSeconds = engineWindowSeconds(spec);
  const sessionToken = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  let state: DictationState = "idle";
  let detector: SpeechDetector | null = null;
  let activeTurn: StreamingTurn | null = null;
  // The turn whose hypothesis the tail shows, so a turn's late words replace its own tail and no other's.
  let tentativeOwner: StreamingTurn | null = null;
  let pendingFinalize: Promise<void> | null = null;
  // Bumped by cancel and dispose, so nothing from before them lands.
  let runId = 0;
  let lastPreviewAt = 0;
  let level = 0;
  let lastFrameAt = 0;
  let preRoll: Float32Array[] = [];
  let channel: DictationChannel | null = null;

  const setState = (next: DictationState): void => {
    if (state === next) return;
    state = next;
    events.onStateChange(next);
  };

  const releaseDetector = async (): Promise<void> => {
    const current = detector;
    detector = null;
    if (current !== null) await current.release();
  };

  const showTentative = (turn: StreamingTurn, words: string): void => {
    tentativeOwner = words === "" ? null : turn;
    events.onTentativeWords(words);
  };

  const clearTentativeOf = (turn: StreamingTurn): void => {
    if (tentativeOwner === turn) {
      tentativeOwner = null;
      events.onTentativeWords("");
    }
  };

  const scheduler = new TranscriptionScheduler((audio) => deps.connect(options).transcribe(audio), {
    onHeld: () => {
      // The words wait, in order, for Retry or Discard; meanwhile nothing more is heard.
      void releaseDetector();
      activeTurn = null;
      setState("idle");
      events.onProblem(HELD_WORDS_PROBLEM);
    },
  });

  const commitChunk = (turn: StreamingTurn, audio: Float32Array, run: number): Promise<void> =>
    scheduler.commit(audio).then(
      (result) => {
        if (runId !== run) return;
        const added = turn.commitText(result.text);
        // A slice emptied the window's hypothesis with it; the next preview refills the tail.
        if (turn === activeTurn) showTentative(turn, turn.tentativeText);
        else clearTentativeOf(turn);
        if (added !== "") events.onFinalWords(added);
      },
      () => undefined,
    );

  const finalizeTurn = async (turn: StreamingTurn, run: number): Promise<void> => {
    for (const chunk of turn.drainChunks()) {
      try {
        const result = await scheduler.commit(chunk);
        if (runId !== run) return;
        const added = turn.commitText(result.text);
        // The tail stays as a bridge until the words that replace it land, then goes with them.
        clearTentativeOf(turn);
        if (added !== "") events.onFinalWords(added);
      } catch (error) {
        if (runId !== run || error instanceof CommitDiscardedError) return;
        return;
      }
    }
    if (runId !== run) return;
    clearTentativeOf(turn);
    if (activeTurn === null) events.onUtteranceEnd();
  };

  const beginTurn = (): void => {
    if (state !== "listening") return;
    const turn = new StreamingTurn(windowSeconds);
    for (const frame of preRoll) turn.addFrame(frame);
    preRoll = [];
    activeTurn = turn;
    lastPreviewAt = deps.now();
  };

  const endTurn = (): void => {
    const turn = activeTurn;
    activeTurn = null;
    if (turn === null) return;
    pendingFinalize = finalizeTurn(turn, runId);
  };

  const abortTurn = (): void => {
    const turn = activeTurn;
    activeTurn = null;
    if (turn !== null) clearTentativeOf(turn);
  };

  const handleFrame = (frame: Float32Array, _speechProbability: number): void => {
    if (state !== "listening") return;
    const run = runId;
    const now = deps.now();
    const dt = lastFrameAt === 0 ? FRAME_MS : Math.min(200, Math.max(1, now - lastFrameAt));
    lastFrameAt = now;
    level = smoothLevel(level, Math.min(1, frameRms(frame) * LEVEL_GAIN), dt);
    events.onLevel(level);
    // The detector reuses its frame buffers; keep a copy.
    const copy = new Float32Array(frame);
    const turn = activeTurn;
    if (turn === null) {
      preRoll.push(copy);
      if (preRoll.length > PRE_ROLL_FRAMES) preRoll.shift();
      return;
    }
    turn.addFrame(copy);
    for (let head = turn.takeHeadSlice(); head !== null; head = turn.takeHeadSlice()) {
      void commitChunk(turn, head, run);
    }
    if (turn.bufferedSamples < PREVIEW_MIN_SAMPLES || now - lastPreviewAt < PREVIEW_INTERVAL_MS) return;
    lastPreviewAt = now;
    const { audio, epoch } = turn.windowAudio();
    void scheduler.preview(audio).then((result) => {
      if (result === null || runId !== run || turn !== activeTurn) return;
      turn.applyHypothesis(result.text, epoch);
      showTentative(turn, turn.tentativeText);
    });
  };

  const ensureChannel = (): void => {
    if (channel !== null) return;
    channel = deps.createChannel();
    if (channel === null) return;
    channel.onmessage = (event: MessageEvent) => {
      const data: unknown = event.data;
      if (typeof data !== "object" || data === null) return;
      const { type, token } = data as { type?: unknown; token?: unknown };
      if (type !== "dictation-started" || token === sessionToken) return;
      // Another dictation started somewhere in the workspace: this one yields, keeping what it heard.
      if (state === "listening") void stop();
      else if (state === "starting") void cancel();
    };
  };

  const fail = (problem: DictationProblem): void => {
    setState("idle");
    events.onProblem(problem);
  };

  const start = async (): Promise<void> => {
    if (state !== "idle") return;
    if (scheduler.isHeld) discardHeld();
    events.onProblem(null);
    runId += 1;
    const run = runId;
    setState("starting");
    try {
      const created = await deps.createDetector({
        assetBase: options.assetBase,
        getStream: deps.getStream,
        onFrame: handleFrame,
        onSpeechStart: beginTurn,
        onSpeechEnd: endTurn,
        onMisfire: abortTurn,
      });
      if (runId !== run) {
        await created.release();
        return;
      }
      detector = created;
      await created.start();
    } catch (error) {
      await releaseDetector();
      if (runId === run) fail(describeMicrophoneError(error, deps.isMicrophoneAllowedByFrame()));
      return;
    }
    if (runId !== run) return;
    ensureChannel();
    channel?.postMessage({ type: "dictation-started", token: sessionToken });
    level = 0;
    lastFrameAt = 0;
    preRoll = [];
    setState("listening");
  };

  const stop = async (): Promise<void> => {
    if (state !== "listening") return;
    const run = runId;
    setState("stopping");
    // The user usually stops right after speaking, before the detector's silence has run out: the open turn is
    // finished rather than dropped (a junk fold discards itself).
    const turn = activeTurn;
    activeTurn = null;
    await releaseDetector();
    if (turn !== null) {
      pendingFinalize = finalizeTurn(turn, run);
    }
    if (pendingFinalize !== null) await pendingFinalize;
    if (runId !== run) return;
    setState("idle");
  };

  const cancel = async (): Promise<void> => {
    runId += 1;
    activeTurn = null;
    pendingFinalize = null;
    scheduler.clear();
    tentativeOwner = null;
    await releaseDetector();
    events.onTentativeWords("");
    events.onUtteranceEnd();
    setState("idle");
  };

  const retryHeld = (): void => {
    if (!scheduler.isHeld) return;
    events.onProblem(null);
    scheduler.retryHeld();
  };

  const discardHeld = (): void => {
    if (!scheduler.isHeld) return;
    events.onProblem(null);
    scheduler.discardHeld();
    events.onTentativeWords("");
    events.onUtteranceEnd();
  };

  return {
    get state() {
      return state;
    },
    get isHeld() {
      return scheduler.isHeld;
    },
    start,
    stop,
    cancel,
    retryHeld,
    discardHeld,
    dispose() {
      runId += 1;
      activeTurn = null;
      pendingFinalize = null;
      scheduler.clear();
      void releaseDetector();
      channel?.close();
      channel = null;
      state = "idle";
    },
  };
}
