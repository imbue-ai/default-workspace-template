/**
 * The chime that says the microphone is open: two short bell tones, played as dictation reaches ``listening``, so
 * a user who pressed the mic and looked away while the model loaded knows the moment to speak. Written against
 * the slice of the Web Audio API it uses, so a test can hand in a fake and read the tones it schedules.
 *
 * A browser lets a page make sound only after a user gesture, so the context is made (or resumed) by ``wake`` from
 * the press that starts dictation, and ``play`` only schedules on what ``wake`` left; where there is no context,
 * or the browser refused one, both stay silent.
 */

export interface AudioParamLike {
  setValueAtTime(value: number, startTime: number): unknown;
  exponentialRampToValueAtTime(value: number, endTime: number): unknown;
}

export interface AudioNodeLike {
  connect(destination: AudioNodeLike): unknown;
}

export interface OscillatorLike extends AudioNodeLike {
  type: string;
  readonly frequency: AudioParamLike;
  start(when: number): void;
  stop(when: number): void;
}

export interface GainLike extends AudioNodeLike {
  readonly gain: AudioParamLike;
}

export interface AudioContextLike {
  readonly currentTime: number;
  readonly state: string;
  readonly destination: AudioNodeLike;
  createOscillator(): OscillatorLike;
  createGain(): GainLike;
  resume(): Promise<void>;
}

export interface ReadyChime {
  /** Make the audio context, or resume one the browser suspended; called from a user gesture. */
  wake(): void;
  /** Two short bell tones, now; silent without a woken context. */
  play(): void;
}

// A bell: C6 with its octave partial, each tone a 70 ms exponential decay, struck twice 110 ms apart.
export const CHIME_FREQUENCY_HZ = 1046.5;
export const CHIME_OCTAVE_GAIN = 0.35;
export const CHIME_PEAK_GAIN = 0.15;
export const CHIME_FLOOR_GAIN = 0.0015;
export const CHIME_TONE_SECONDS = 0.07;
export const CHIME_SECOND_TONE_OFFSET_SECONDS = 0.11;

function scheduleTone(context: AudioContextLike, at: number): void {
  const envelope = context.createGain();
  envelope.gain.setValueAtTime(CHIME_PEAK_GAIN, at);
  envelope.gain.exponentialRampToValueAtTime(CHIME_FLOOR_GAIN, at + CHIME_TONE_SECONDS);
  envelope.connect(context.destination);
  const partials: [number, AudioNodeLike][] = [[CHIME_FREQUENCY_HZ, envelope]];
  const octave = context.createGain();
  octave.gain.setValueAtTime(CHIME_OCTAVE_GAIN, at);
  octave.connect(envelope);
  partials.push([CHIME_FREQUENCY_HZ * 2, octave]);
  for (const [frequency, into] of partials) {
    const oscillator = context.createOscillator();
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(frequency, at);
    oscillator.connect(into);
    oscillator.start(at);
    oscillator.stop(at + CHIME_TONE_SECONDS);
  }
}

export function createReadyChime(createContext: () => AudioContextLike = () => new AudioContext()): ReadyChime {
  let context: AudioContextLike | null = null;
  return {
    wake() {
      try {
        context ??= createContext();
        if (context.state === "suspended") context.resume().catch(() => undefined);
      } catch {
        context = null;
      }
    },
    play() {
      if (context === null) return;
      try {
        const at = context.currentTime;
        scheduleTone(context, at);
        scheduleTone(context, at + CHIME_SECOND_TONE_OFFSET_SECONDS);
      } catch {
        // A context the browser closed under the page: the words still land, only the cue is missed.
      }
    },
  };
}
