/**
 * Opening the microphone, and what to tell the user when it cannot be (the workspace-voice-input spec, D5 and
 * the failure modes): each way it fails gets its own plain sentence, and Retry is offered only where trying again
 * can help.
 */

export type MicrophoneProblemKind = "frame_blocked" | "blocked" | "missing" | "busy" | "failed";

export interface MicrophoneProblem {
  readonly kind: MicrophoneProblemKind;
  readonly message: string;
  /** Whether trying again, with nothing changed, can succeed. */
  readonly canRetry: boolean;
}

export const MICROPHONE_CONSTRAINTS: MediaStreamConstraints = {
  audio: { channelCount: 1, echoCancellation: true, autoGainControl: true, noiseSuppression: true },
};

const BLOCKED_NAMES = new Set(["NotAllowedError", "PermissionDeniedError", "SecurityError"]);
const MISSING_NAMES = new Set(["NotFoundError", "DevicesNotFoundError", "OverconstrainedError"]);
const BUSY_NAMES = new Set(["NotReadableError", "TrackStartError", "AbortError"]);

function errorName(error: unknown): string {
  return typeof error === "object" && error !== null && "name" in error && typeof error.name === "string"
    ? error.name
    : "";
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message !== "") return error.message;
  if (typeof error === "string" && error !== "") return error;
  return "unknown error";
}

/**
 * The problem a failed ``getUserMedia`` is, in the user's terms. ``isAllowedByFrame`` is what the page's
 * permissions policy says about the microphone (null where the browser does not say), which tells a frame that
 * was never given the microphone from a user who blocked it.
 */
export function describeMicrophoneError(error: unknown, isAllowedByFrame: boolean | null): MicrophoneProblem {
  const name = errorName(error);
  if (BLOCKED_NAMES.has(name)) {
    if (isAllowedByFrame === false) {
      return {
        kind: "frame_blocked",
        message: "This page hasn't been given the microphone by the window around it, so it can't listen here.",
        canRetry: false,
      };
    }
    return {
      kind: "blocked",
      message:
        "The microphone is blocked. Allow it for this page in your browser's site settings (and, on a Mac, " +
        "for this app in System Settings > Privacy & Security > Microphone), then try again.",
      canRetry: false,
    };
  }
  if (MISSING_NAMES.has(name)) {
    return {
      kind: "missing",
      message: "No microphone was found. Plug one in or choose one in your system's sound settings, then try again.",
      canRetry: true,
    };
  }
  if (BUSY_NAMES.has(name)) {
    return {
      kind: "busy",
      message: "The microphone is in use by another app. Close it or stop its recording, then try again.",
      canRetry: true,
    };
  }
  return { kind: "failed", message: `The microphone couldn't be started: ${errorMessage(error)}.`, canRetry: true };
}

/** What the page's permissions policy says about the microphone: null where the browser has no such API. */
export function isMicrophoneAllowedByFrame(document: Document): boolean | null {
  const policy = (document as Document & { featurePolicy?: { allowsFeature(feature: string): boolean } })
    .featurePolicy;
  if (policy === undefined) return null;
  try {
    return policy.allowsFeature("microphone");
  } catch {
    return null;
  }
}
