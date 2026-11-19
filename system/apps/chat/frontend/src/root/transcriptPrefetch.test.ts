/**
 * The chat root's transcript reads for the pages it frames: started once per chat with a transcript, handed over
 * once, forgotten with the frame, and a failure left for the page to report rather than surfacing on its own.
 */
import { describe, expect, it, vi } from "vitest";
import type { EventsResponse } from "../models/Response";
import { TranscriptPrefetch } from "./transcriptPrefetch";

const SNAPSHOT: EventsResponse = { events: [], offset: 0, total: 0 };

describe("TranscriptPrefetch", () => {
  it("hands the read it started to the first taker only", async () => {
    const read = vi.fn(() => Promise.resolve(SNAPSHOT));
    const transcripts = new TranscriptPrefetch(read, () => true);

    transcripts.start("agent-a");
    const taken = transcripts.take("agent-a");

    expect(read).toHaveBeenCalledWith("agent-a");
    expect(await taken).toBe(SNAPSHOT);
    expect(transcripts.take("agent-a")).toBeNull();
  });

  it("starts one read per chat while it waits for its page", () => {
    const read = vi.fn(() => Promise.resolve(SNAPSHOT));
    const transcripts = new TranscriptPrefetch(read, () => true);

    transcripts.start("agent-a");
    transcripts.start("agent-a");

    expect(read).toHaveBeenCalledTimes(1);
  });

  it("reads nothing for a chat with no transcript to read yet", () => {
    const read = vi.fn(() => Promise.resolve(SNAPSHOT));
    const transcripts = new TranscriptPrefetch(read, (chatId) => chatId !== "agent-provisional");

    transcripts.start("agent-provisional");

    expect(read).not.toHaveBeenCalled();
    expect(transcripts.take("agent-provisional")).toBeNull();
  });

  it("forgets a dropped chat's read", () => {
    const transcripts = new TranscriptPrefetch(
      () => Promise.resolve(SNAPSHOT),
      () => true,
    );

    transcripts.start("agent-a");
    transcripts.drop("agent-a");

    expect(transcripts.take("agent-a")).toBeNull();
  });

  it("leaves a failed read for its taker to report, without an unhandled rejection meanwhile", async () => {
    const failure = new Error("offline");
    const unhandled = vi.fn();
    process.on("unhandledRejection", unhandled);
    try {
      const transcripts = new TranscriptPrefetch(
        () => Promise.reject(failure),
        () => true,
      );
      transcripts.start("agent-a");
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(unhandled).not.toHaveBeenCalled();
      await expect(transcripts.take("agent-a")).rejects.toBe(failure);
    } finally {
      process.off("unhandledRejection", unhandled);
    }
  });
});
