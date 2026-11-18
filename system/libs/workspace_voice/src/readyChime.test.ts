import { describe, expect, it, vi } from "vitest";
import {
  CHIME_FLOOR_GAIN,
  CHIME_FREQUENCY_HZ,
  CHIME_PEAK_GAIN,
  CHIME_SECOND_TONE_OFFSET_SECONDS,
  CHIME_TONE_SECONDS,
  createReadyChime,
} from "./readyChime";
import type { AudioContextLike, AudioNodeLike, AudioParamLike } from "./readyChime";

interface FakeParam extends AudioParamLike {
  readonly sets: [number, number][];
  readonly ramps: [number, number][];
}

interface FakeOscillator {
  type: string;
  frequency: FakeParam;
  startedAt: number | null;
  stoppedAt: number | null;
  connectedTo: AudioNodeLike | null;
}

function fakeParam(): FakeParam {
  const param: FakeParam = {
    sets: [],
    ramps: [],
    setValueAtTime: (value, at) => void param.sets.push([value, at]),
    exponentialRampToValueAtTime: (value, at) => void param.ramps.push([value, at]),
  };
  return param;
}

/** An audio context that records what is scheduled on it. */
function fakeContext(state = "running") {
  const oscillators: FakeOscillator[] = [];
  const gains: { gain: FakeParam; connectedTo: AudioNodeLike | null }[] = [];
  const destination: AudioNodeLike = { connect: () => undefined };
  const resume = vi.fn(async () => {
    context.state = "running";
  });
  const context: AudioContextLike & { state: string } = {
    currentTime: 2.5,
    state,
    destination,
    resume,
    createOscillator() {
      const oscillator: FakeOscillator & AudioNodeLike & { start(when: number): void; stop(when: number): void } = {
        type: "",
        frequency: fakeParam(),
        startedAt: null,
        stoppedAt: null,
        connectedTo: null,
        connect(into) {
          oscillator.connectedTo = into;
        },
        start(when) {
          oscillator.startedAt = when;
        },
        stop(when) {
          oscillator.stoppedAt = when;
        },
      };
      oscillators.push(oscillator);
      return oscillator;
    },
    createGain() {
      const node: { gain: FakeParam; connectedTo: AudioNodeLike | null } & AudioNodeLike = {
        gain: fakeParam(),
        connectedTo: null,
        connect(into) {
          node.connectedTo = into;
        },
      };
      gains.push(node);
      return node;
    },
  };
  return { context, oscillators, gains, destination, resume };
}

describe("the ready chime", () => {
  it("strikes two bell tones, each a sine with its octave partial under one decaying envelope", () => {
    const { context, oscillators, gains, destination } = fakeContext();
    const chime = createReadyChime(() => context);
    chime.wake();
    chime.play();
    const first = context.currentTime;
    const second = first + CHIME_SECOND_TONE_OFFSET_SECONDS;
    expect(oscillators.map((oscillator) => oscillator.startedAt)).toEqual([first, first, second, second]);
    expect(oscillators.map((oscillator) => oscillator.frequency.sets[0][0])).toEqual([
      CHIME_FREQUENCY_HZ,
      CHIME_FREQUENCY_HZ * 2,
      CHIME_FREQUENCY_HZ,
      CHIME_FREQUENCY_HZ * 2,
    ]);
    expect(oscillators.every((oscillator) => oscillator.type === "sine")).toBe(true);
    expect(oscillators.map((oscillator) => oscillator.stoppedAt)).toEqual(
      [first, first, second, second].map((at) => at + CHIME_TONE_SECONDS),
    );
    // Each tone's envelope sits on the destination; the octave's own gain feeds the envelope.
    const envelopes = gains.filter((gain) => gain.connectedTo === destination);
    expect(envelopes.length).toBe(2);
    expect(envelopes.map((envelope) => envelope.gain.sets[0])).toEqual([
      [CHIME_PEAK_GAIN, first],
      [CHIME_PEAK_GAIN, second],
    ]);
    expect(envelopes.map((envelope) => envelope.gain.ramps[0])).toEqual([
      [CHIME_FLOOR_GAIN, first + CHIME_TONE_SECONDS],
      [CHIME_FLOOR_GAIN, second + CHIME_TONE_SECONDS],
    ]);
    const octaves = gains.filter((gain) => gain.connectedTo !== destination);
    expect(octaves.map((octave) => octave.connectedTo)).toEqual(envelopes);
    expect(oscillators[1].connectedTo).toBe(octaves[0]);
    expect(oscillators[0].connectedTo).toBe(envelopes[0]);
  });

  it("makes the context once, on the first wake, and resumes one the browser suspended", () => {
    const { context, resume } = fakeContext("suspended");
    const createContext = vi.fn(() => context);
    const chime = createReadyChime(createContext);
    chime.wake();
    chime.wake();
    expect(createContext).toHaveBeenCalledTimes(1);
    expect(resume).toHaveBeenCalledTimes(1);
  });

  it("stays silent before any wake, and where the browser gives no context", () => {
    const { context, oscillators } = fakeContext();
    const chime = createReadyChime(() => context);
    chime.play();
    expect(oscillators.length).toBe(0);
    const refused = createReadyChime(() => {
      throw new Error("AudioContext is not defined");
    });
    expect(() => {
      refused.wake();
      refused.play();
    }).not.toThrow();
  });

  it("swallows a context that fails to schedule", () => {
    const { context } = fakeContext();
    context.createOscillator = () => {
      throw new Error("closed");
    };
    const chime = createReadyChime(() => context);
    chime.wake();
    expect(() => chime.play()).not.toThrow();
  });
});
