import { describe, expect, it } from "vitest";
import {
  STREAMING_TURN_DEFAULTS,
  StreamingTurn,
  cleanAsrText,
  collapseRepeats,
  dropSeamOverlap,
  frameRms,
} from "./streamingTurn";

const SAMPLE_RATE = STREAMING_TURN_DEFAULTS.sampleRate;
const FRAME_SAMPLES = 512;
const SOFT_CAP_SAMPLES = STREAMING_TURN_DEFAULTS.softCapSeconds * SAMPLE_RATE;

function makeFrame(level: number): Float32Array {
  const frame = new Float32Array(FRAME_SAMPLES);
  frame.fill(level);
  return frame;
}

function secondsToFrames(seconds: number): number {
  return Math.ceil((seconds * SAMPLE_RATE) / FRAME_SAMPLES);
}

/** Feed frames of one level, collecting any head slice that comes due. */
function feed(turn: StreamingTurn, level: number, frames: number, onSlice: (head: Float32Array) => void): void {
  for (let i = 0; i < frames; i += 1) {
    turn.addFrame(makeFrame(level));
    const head = turn.takeHeadSlice();
    if (head !== null) onSlice(head);
  }
}

/** A long turn with real pauses: loud stretches separated by 0.3 s of quiet. Answers the samples fed. */
function speakLongTurnWithPauses(turn: StreamingTurn, onSlice: (head: Float32Array) => void): number {
  const loudFrames = secondsToFrames(12);
  const quietFrames = secondsToFrames(0.3);
  feed(turn, 0.1, loudFrames, onSlice);
  feed(turn, 0, quietFrames, onSlice);
  feed(turn, 0.1, loudFrames, onSlice);
  feed(turn, 0, quietFrames, onSlice);
  feed(turn, 0.1, loudFrames, onSlice);
  return (3 * loudFrames + 2 * quietFrames) * FRAME_SAMPLES;
}

describe("StreamingTurn commit-and-slice", () => {
  it("commits without loss or duplication across window slides", () => {
    const turn = new StreamingTurn();
    const chunks: Float32Array[] = [];
    let counter = 0;
    const commitChunk = (chunk: Float32Array): void => {
      counter += 1;
      turn.commitText(`chunk${counter}`);
      chunks.push(chunk);
    };
    const fedSamples = speakLongTurnWithPauses(turn, commitChunk);
    const sliceCount = counter;
    for (const chunk of turn.drainChunks()) commitChunk(chunk);

    expect(sliceCount).toBeGreaterThanOrEqual(2);
    expect(turn.committedText).toBe(Array.from({ length: counter }, (_, i) => `chunk${i + 1}`).join(" "));
    expect(chunks.reduce((total, chunk) => total + chunk.length, 0)).toBe(fedSamples);
    expect(turn.bufferedSamples).toBe(0);
  });

  it("keeps every chunk within the soft cap when pauses exist, cut in silence so no word is split", () => {
    const turn = new StreamingTurn();
    const heads: Float32Array[] = [];
    speakLongTurnWithPauses(turn, (head) => heads.push(head));
    expect(heads.length).toBeGreaterThanOrEqual(1);
    for (const head of heads) {
      expect(head.length).toBeLessThanOrEqual(SOFT_CAP_SAMPLES);
      expect(head[head.length - 1]).toBe(0);
    }
    for (const chunk of turn.drainChunks()) expect(chunk.length).toBeLessThanOrEqual(SOFT_CAP_SAMPLES);
  });

  it("does not take a short dip for a pause: no cut before the hard cap", () => {
    const turn = new StreamingTurn();
    let sliceCount = 0;
    const onSlice = (): void => {
      sliceCount += 1;
    };
    feed(turn, 0.1, secondsToFrames(10), onSlice);
    // A 100 ms dip is a stop consonant's closure, not a gap between words.
    feed(turn, 0, secondsToFrames(0.1), onSlice);
    feed(turn, 0.1, secondsToFrames(12), onSlice);
    expect(sliceCount).toBe(0);
    expect(turn.bufferedSamples).toBeGreaterThan(SOFT_CAP_SAMPLES);
    feed(turn, 0.1, secondsToFrames(9), onSlice);
    expect(sliceCount).toBeGreaterThanOrEqual(1);
  });

  it("loses nothing under forced cuts when speech never pauses", () => {
    const turn = new StreamingTurn();
    const chunks: Float32Array[] = [];
    const frames = secondsToFrames(32);
    feed(turn, 0.1, frames, (head) => chunks.push(head));
    chunks.push(...turn.drainChunks());
    expect(chunks.reduce((total, chunk) => total + chunk.length, 0)).toBe(frames * FRAME_SAMPLES);
    expect(chunks.length).toBeGreaterThanOrEqual(2);
  });

  it("takes the window from the engine's own caps", () => {
    const turn = new StreamingTurn({ softCapSeconds: 4, hardCapSeconds: 6 });
    let sliceCount = 0;
    feed(turn, 0.1, secondsToFrames(3), () => (sliceCount += 1));
    feed(turn, 0, secondsToFrames(0.3), () => (sliceCount += 1));
    feed(turn, 0.1, secondsToFrames(2), () => (sliceCount += 1));
    expect(sliceCount).toBe(1);
  });

  it("de-duplicates the seam case- and punctuation-insensitively when folding, and tells what was added", () => {
    const turn = new StreamingTurn();
    expect(turn.commitText("Hello, world.")).toBe("Hello, world.");
    expect(turn.commitText("world again")).toBe("again");
    expect(turn.committedText).toBe("Hello, world. again");
  });

  it("reads the live text as committed words then the window hypothesis", () => {
    const turn = new StreamingTurn();
    turn.commitText("first part");
    turn.applyHypothesis("second bit", turn.epoch);
    expect(turn.liveText).toBe("first part second bit");
    expect(turn.tentativeText).toBe("second bit");
  });

  it("ignores a hypothesis from before a slice", () => {
    const turn = new StreamingTurn();
    const staleEpoch = turn.epoch;
    let didSlice = false;
    speakLongTurnWithPauses(turn, () => {
      didSlice = true;
    });
    expect(didSlice).toBe(true);
    expect(turn.epoch).not.toBe(staleEpoch);
    turn.applyHypothesis("stale words", staleEpoch);
    expect(turn.liveText).toBe("");
  });

  it("never lets a hypothesis touch the committed transcript", () => {
    const turn = new StreamingTurn();
    turn.applyHypothesis("ghost words", turn.epoch);
    expect(turn.liveText).toBe("ghost words");
    expect(turn.committedText).toBe("");
  });

  it("drains the window at the end of the turn so committedText is the transcript", () => {
    const turn = new StreamingTurn();
    feed(turn, 0.1, secondsToFrames(3), () => undefined);
    const chunks = turn.drainChunks();
    expect(chunks.length).toBe(1);
    turn.commitText("the whole thing");
    expect(turn.committedText).toBe("the whole thing");
    expect(turn.bufferedSamples).toBe(0);
  });

  it("commits nothing for junk", () => {
    const turn = new StreamingTurn();
    expect(turn.commitText("[BLANK_AUDIO]")).toBe("");
    turn.commitText("(coughs)");
    turn.commitText("   ");
    expect(turn.committedText).toBe("");
  });
});

describe("text hygiene", () => {
  it("cleanAsrText strips annotations and rejects junk", () => {
    expect(cleanAsrText("[BLANK_AUDIO]")).toBe("");
    expect(cleanAsrText("(coughs) hello there")).toBe("hello there");
    expect(cleanAsrText("...")).toBe("");
  });

  it("collapseRepeats reduces a runaway loop but keeps natural repetition", () => {
    expect(collapseRepeats("yes yes yes yes yes")).toBe("yes");
    expect(collapseRepeats("no no no")).toBe("no no no");
    expect(collapseRepeats("a b a b a b")).toBe("a b");
  });

  it("dropSeamOverlap drops only a repeated seam", () => {
    expect(dropSeamOverlap(["hello", "world"], ["world", "again"])).toEqual(["again"]);
    expect(dropSeamOverlap(["hello", "world"], ["brand", "new"])).toEqual(["brand", "new"]);
  });

  it("frameRms measures a frame's level", () => {
    expect(frameRms(makeFrame(0.1))).toBeCloseTo(0.1, 5);
    expect(frameRms(makeFrame(0))).toBe(0);
  });
});
