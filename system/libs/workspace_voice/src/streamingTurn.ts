/**
 * The streaming model of one utterance (a "turn"), ported from Sculptor's voice entry, itself a port of
 * Local-Vocal (the workspace-voice-input spec, D3). It knows nothing of the microphone, the engine, or scheduling.
 *
 * Silence-anchored commit-and-slice: a turn holds the words already committed (which grow without bound) and a
 * rolling window of audio bounded to the engine's soft cap. The driver transcribes the whole window for the live
 * preview, so each preview costs the same however long the turn runs. Once the window passes the soft cap, the
 * turn slices a head off it at a real pause (a quiet run of at least ``minPauseMs``: an inter-word gap, never a
 * stop-consonant closure), hands that head out to be transcribed, and folds the words into the committed text.
 * The cut lands in silence, so no word is split; the seam de-duplication in ``commitText`` backstops the forced
 * cut of a turn that never pauses before the hard cap. At the end of the turn the rest of the window drains the
 * same way, so the turn's transcript is simply ``committedText`` once the last chunk folds in, and the preview
 * is always committed text plus the window's hypothesis: the same words the final will read, read earlier.
 *
 * Cuts come from audio energy because the first engine gives no word timestamps.
 */

const PUNCTUATION = /[.,!?;:"'’]/g;

function normalizeWord(word: string): string {
  return word.replace(PUNCTUATION, "").toLowerCase();
}

// A runaway decoder repeats one phrase many times; collapse it to one copy. The shortest repeating phrase wins
// (the loop's true period); a single word needs a higher bar so natural emphasis ("no no no") survives.
const MAX_PHRASE_WORDS = 30;
const MIN_REPEATS_MULTIWORD = 3;
const MIN_REPEATS_SINGLE_WORD = 4;

function rangesEqual(words: readonly string[], a: number, b: number, length: number): boolean {
  for (let k = 0; k < length; k += 1) {
    if (words[a + k] !== words[b + k]) return false;
  }
  return true;
}

export function collapseRepeats(text: string): string {
  const words = text.split(/\s+/).filter(Boolean);
  const count = words.length;
  if (count < 2) return text.trim();
  const normalized = words.map(normalizeWord);
  const out: string[] = [];
  let i = 0;
  while (i < count) {
    let didCollapse = false;
    const maxLength = Math.min(MAX_PHRASE_WORDS, Math.floor((count - i) / 2));
    for (let length = 1; length <= maxLength; length += 1) {
      let repeats = 1;
      let j = i + length;
      while (j + length <= count && rangesEqual(normalized, i, j, length)) {
        repeats += 1;
        j += length;
      }
      const threshold = length === 1 ? MIN_REPEATS_SINGLE_WORD : MIN_REPEATS_MULTIWORD;
      if (repeats >= threshold) {
        for (let k = i; k < i + length; k += 1) out.push(words[k]);
        i = j;
        didCollapse = true;
        break;
      }
    }
    if (!didCollapse) {
      out.push(words[i]);
      i += 1;
    }
  }
  return out.join(" ");
}

/** Strip the bracketed annotations a model writes on non-speech audio ("[BLANK_AUDIO]", "(coughs)"); text with
 *  nothing pronounceable left is junk and reads as ""; a runaway loop collapses to one copy. */
export function cleanAsrText(text: string): string {
  const stripped = text.replace(/\[[^\]]*\]|\([^)]*\)/g, " ");
  if (!/[a-z0-9]/i.test(stripped)) return "";
  return collapseRepeats(stripped.replace(/\s+/g, " ").trim());
}

// A chunk folding onto the committed words drops its leading words when they repeat the committed tail. A cut at
// a pause shares no audio, so overlap is rare; the forced cut is where the engine can hear the boundary word in
// both chunks. Capped so a long repetition the speaker meant is never eaten.
const MAX_SEAM_OVERLAP_WORDS = 6;

export function dropSeamOverlap(committedWords: readonly string[], newWords: readonly string[]): string[] {
  const maxK = Math.min(MAX_SEAM_OVERLAP_WORDS, committedWords.length, newWords.length);
  for (let k = maxK; k >= 1; k -= 1) {
    let doesMatch = true;
    for (let i = 0; i < k; i += 1) {
      if (normalizeWord(committedWords[committedWords.length - k + i]) !== normalizeWord(newWords[i])) {
        doesMatch = false;
        break;
      }
    }
    if (doesMatch) return newWords.slice(k);
  }
  return [...newWords];
}

export function frameRms(frame: Float32Array): number {
  let sum = 0;
  for (let i = 0; i < frame.length; i += 1) sum += frame[i] * frame[i];
  return Math.sqrt(sum / Math.max(1, frame.length));
}

export function concatFrames(chunks: readonly Float32Array[]): Float32Array {
  let total = 0;
  for (const chunk of chunks) total += chunk.length;
  const out = new Float32Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

export interface StreamingTurnOptions {
  sampleRate: number;
  /** Past this much window, look for a pause to slice at (the engine's call limit less a margin). */
  softCapSeconds: number;
  /** Waiting for a real pause may let the window grow past the soft cap; here a cut is forced at the quietest
   *  frame rather than waiting longer: the most audio one call may carry. */
  hardCapSeconds: number;
  /** A qualifying pause is a quiet run at least this long. Shorter dips sit inside words. */
  minPauseMs: number;
  /** Never slice off a sliver: a head is at least this long. */
  minHeadSeconds: number;
  /** The forced cut hunts for the quietest frame this far back from the soft cap. */
  forcedCutSearchSeconds: number;
  /** A frame is quiet under both an absolute floor and a fraction of the loudest frame of the turn (microphone
   *  gains vary widely). */
  quietAbsoluteRms: number;
  quietPeakFraction: number;
}

export const STREAMING_TURN_DEFAULTS: StreamingTurnOptions = {
  sampleRate: 16000,
  softCapSeconds: 20,
  hardCapSeconds: 30,
  minPauseMs: 200,
  minHeadSeconds: 1,
  forcedCutSearchSeconds: 4,
  quietAbsoluteRms: 0.006,
  quietPeakFraction: 0.08,
};

export interface WindowAudio {
  audio: Float32Array;
  epoch: number;
}

/**
 * The per-turn streaming state. Pure and synchronous: the driver owns the engine and feeds results back in.
 * The contract: ``addFrame`` for every frame of the turn; ``takeHeadSlice`` after feeding, where non-null means
 * "transcribe this and pass the text to ``commitText``" (folds land in slice order when the driver runs commits
 * one at a time in order); ``windowAudio`` for a preview, whose text goes to ``applyHypothesis`` with the epoch it
 * came with (a slice in between makes it stale); and at the end of the turn ``drainChunks`` empties the window,
 * after which committing each chunk's text makes ``committedText`` the turn's transcript.
 */
export class StreamingTurn {
  private readonly softCap: number;
  private readonly hardCap: number;
  private readonly minPauseSamples: number;
  private readonly minHead: number;
  private readonly forcedSearch: number;
  private readonly quietAbs: number;
  private readonly quietFraction: number;

  private committed: string[] = [];
  private frames: Float32Array[] = [];
  private frameLevels: number[] = [];
  private samples = 0;
  private peakRms = 0;
  private turnEpoch = 0;
  private hypothesis: string[] = [];

  constructor(options?: Partial<StreamingTurnOptions>) {
    const resolved = { ...STREAMING_TURN_DEFAULTS, ...options };
    this.softCap = Math.round(resolved.softCapSeconds * resolved.sampleRate);
    this.hardCap = Math.round(resolved.hardCapSeconds * resolved.sampleRate);
    this.minPauseSamples = Math.round((resolved.minPauseMs / 1000) * resolved.sampleRate);
    this.minHead = Math.round(resolved.minHeadSeconds * resolved.sampleRate);
    this.forcedSearch = Math.round(resolved.forcedCutSearchSeconds * resolved.sampleRate);
    this.quietAbs = resolved.quietAbsoluteRms;
    this.quietFraction = resolved.quietPeakFraction;
  }

  /** Feed one frame at the configured sample rate. The frame is kept; pass a copy if the caller reuses its buffer. */
  addFrame(frame: Float32Array): void {
    const rms = frameRms(frame);
    this.frames.push(frame);
    this.frameLevels.push(rms);
    this.samples += frame.length;
    if (rms > this.peakRms) this.peakRms = rms;
  }

  get bufferedSamples(): number {
    return this.samples;
  }

  get epoch(): number {
    return this.turnEpoch;
  }

  get committedText(): string {
    return this.committed.join(" ");
  }

  /** A copy of the window, tagged with the epoch the resulting hypothesis must come back with. */
  windowAudio(): WindowAudio {
    return { audio: concatFrames(this.frames), epoch: this.turnEpoch };
  }

  /** Take one live hypothesis over the window into the preview; never the committed words, which come only
   *  from ``commitText``. A hypothesis from before a slice is ignored. */
  applyHypothesis(text: string, epoch: number): void {
    if (epoch !== this.turnEpoch) return;
    this.hypothesis = cleanAsrText(text).split(" ").filter(Boolean);
  }

  /** The window's current hypothesis, the part of the live text that is not yet final. */
  get tentativeText(): string {
    return this.hypothesis.join(" ");
  }

  /** The live text at any moment and any length: the committed words, then the window's hypothesis. */
  get liveText(): string {
    return [this.committedText, this.tentativeText].filter(Boolean).join(" ");
  }

  /** Fold a transcribed chunk (a head slice or a drained chunk) into the committed words: junk dropped, loops
   *  collapsed, the seam de-duplicated. Answers the words that were added. */
  commitText(rawText: string): string {
    const text = cleanAsrText(rawText);
    if (!text) return "";
    const words = dropSeamOverlap(this.committed, text.split(" "));
    for (const word of words) this.committed.push(word);
    return words.join(" ");
  }

  /** If the window is over the soft cap and a safe cut exists, slice the head off: the audio leaves the window
   *  and comes back for transcribe-and-commit. Null while no cut is due, or while over the soft cap but under the
   *  hard cap with no qualifying pause yet (never cut inside a word). */
  takeHeadSlice(): Float32Array | null {
    if (this.samples <= this.softCap) return null;
    let cutFrame = this.findPauseCutFrame();
    if (cutFrame === null) {
      if (this.samples < this.hardCap) return null;
      cutFrame = this.forcedCutFrame();
    }
    return this.sliceAt(cutFrame);
  }

  /** Empty the window as transcribe-and-commit chunks, each cut at a pause where one exists and none over the
   *  soft cap (but for a single over-long frame). For the end of a turn: commit each chunk's text in order, and
   *  ``committedText`` is then the whole transcript. */
  drainChunks(): Float32Array[] {
    const chunks: Float32Array[] = [];
    while (this.samples > this.softCap) {
      let cutFrame = this.findPauseCutFrame();
      if (cutFrame === null) cutFrame = this.forcedCutFrame();
      const head = this.sliceAt(cutFrame);
      if (head === null) break;
      chunks.push(head);
    }
    if (this.samples > 0) {
      chunks.push(concatFrames(this.frames));
      this.dropWindow(this.frames.length);
    }
    return chunks;
  }

  private quietThreshold(): number {
    return Math.max(this.quietAbs, this.quietFraction * this.peakRms);
  }

  private frameOffsets(): number[] {
    const offsets = new Array<number>(this.frames.length + 1);
    let offset = 0;
    for (let i = 0; i < this.frames.length; i += 1) {
      offsets[i] = offset;
      offset += this.frames[i].length;
    }
    offsets[this.frames.length] = offset;
    return offsets;
  }

  /** The latest real pause whose cut point fits in [minHead, softCap]: a run of quiet frames at least minPause
   *  long, cut at its middle (silence on both sides, so between words by construction), walked back inside the
   *  run if the middle overshoots the cap. */
  private findPauseCutFrame(): number | null {
    const threshold = this.quietThreshold();
    const offsets = this.frameOffsets();
    const count = this.frames.length;
    let best: number | null = null;
    let runStart = -1;
    for (let i = 0; i <= count; i += 1) {
      const isQuiet = i < count && this.frameLevels[i] <= threshold;
      if (isQuiet) {
        if (runStart < 0) runStart = i;
        continue;
      }
      if (runStart >= 0) {
        const runEnd = i;
        if (offsets[runEnd] - offsets[runStart] >= this.minPauseSamples) {
          let cut = runStart + Math.floor((runEnd - runStart) / 2);
          while (cut > runStart && offsets[cut] > this.softCap) cut -= 1;
          const at = offsets[cut];
          if (at >= this.minHead && at <= this.softCap) best = cut;
        }
        runStart = -1;
      }
    }
    return best;
  }

  /** No pause by the hard cap: the quietest single frame near the soft cap, with the seam de-duplication to
   *  catch a word heard on both sides. */
  private forcedCutFrame(): number | null {
    const offsets = this.frameOffsets();
    const from = Math.max(this.minHead, this.softCap - this.forcedSearch);
    let cut: number | null = null;
    let best = Infinity;
    let lastInCap: number | null = null;
    for (let i = 0; i < this.frames.length; i += 1) {
      const at = offsets[i];
      if (at > this.softCap) break;
      if (at >= this.minHead) lastInCap = i;
      if (at >= from && this.frameLevels[i] < best) {
        best = this.frameLevels[i];
        cut = i;
      }
    }
    return cut !== null ? cut : lastInCap;
  }

  private sliceAt(cutFrame: number | null): Float32Array | null {
    if (cutFrame === null || cutFrame <= 0) return null;
    const head = concatFrames(this.frames.slice(0, cutFrame));
    this.dropWindow(cutFrame);
    return head;
  }

  /** Drop the first ``count`` frames and forget the window's hypothesis, which described audio that is no
   *  longer the window. */
  private dropWindow(count: number): void {
    this.frames = this.frames.slice(count);
    this.frameLevels = this.frameLevels.slice(count);
    this.samples = 0;
    for (const frame of this.frames) this.samples += frame.length;
    this.turnEpoch += 1;
    this.hypothesis = [];
  }
}
