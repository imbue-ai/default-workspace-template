import { describe, expect, it } from "vitest";
import { describeMicrophoneError, isMicrophoneAllowedByFrame } from "./microphone";

function named(name: string, message = ""): Error {
  return Object.assign(new Error(message), { name });
}

describe("describeMicrophoneError", () => {
  it("tells a frame that was never given the microphone from a user who blocked it", () => {
    expect(describeMicrophoneError(named("NotAllowedError"), false)).toMatchObject({
      kind: "frame_blocked",
      canRetry: false,
    });
    expect(describeMicrophoneError(named("NotAllowedError"), true)).toMatchObject({
      kind: "blocked",
      canRetry: false,
    });
    expect(describeMicrophoneError(named("NotAllowedError"), null).message).toMatch(/site settings/);
  });

  it("tells a missing microphone from a busy one, both worth a retry", () => {
    expect(describeMicrophoneError(named("NotFoundError"), null)).toMatchObject({ kind: "missing", canRetry: true });
    expect(describeMicrophoneError(named("NotReadableError"), null)).toMatchObject({ kind: "busy", canRetry: true });
  });

  it("carries an unknown failure's own words", () => {
    expect(describeMicrophoneError(new Error("worklet exploded"), null)).toEqual({
      kind: "failed",
      message: "The microphone couldn't be started: worklet exploded.",
      canRetry: true,
    });
    expect(describeMicrophoneError(undefined, null).message).toBe(
      "The microphone couldn't be started: unknown error.",
    );
  });

  it("reads the frame's permission where the browser offers it", () => {
    expect(isMicrophoneAllowedByFrame({} as Document)).toBeNull();
    const document = { featurePolicy: { allowsFeature: (feature: string) => feature === "microphone" } };
    expect(isMicrophoneAllowedByFrame(document as unknown as Document)).toBe(true);
  });
});
