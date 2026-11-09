import { describe, expect, it, vi } from "vitest";
import { CommitDiscardedError, TranscriptionScheduler } from "./scheduler";
import type { TranscriptionResult } from "./speechEngine";

/** A transcriber whose calls the test settles by hand, in any order. */
function manualTranscriber() {
  const calls: { audio: Float32Array; resolve(text: string): void; reject(error: Error): void }[] = [];
  const transcribe = (audio: Float32Array): Promise<TranscriptionResult> =>
    new Promise((resolve, reject) => {
      calls.push({ audio, resolve: (text) => resolve({ text, words: null }), reject });
    });
  return { calls, transcribe };
}

function audio(label: number): Float32Array {
  return new Float32Array([label]);
}

async function settled(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
}

describe("TranscriptionScheduler", () => {
  it("runs one job at a time and resolves commits in audio order", async () => {
    const { calls, transcribe } = manualTranscriber();
    const scheduler = new TranscriptionScheduler(transcribe);
    const order: string[] = [];
    const first = scheduler.commit(audio(1)).then((result) => order.push(result.text));
    const second = scheduler.commit(audio(2)).then((result) => order.push(result.text));
    expect(calls.length).toBe(1);
    expect(scheduler.pendingCommitCount).toBe(2);
    calls[0].resolve("one");
    await settled();
    expect(calls.length).toBe(2);
    calls[1].resolve("two");
    await Promise.all([first, second]);
    expect(order).toEqual(["one", "two"]);
    expect(scheduler.pendingCommitCount).toBe(0);
  });

  it("parks previews behind commits and runs only the newest", async () => {
    const { calls, transcribe } = manualTranscriber();
    const scheduler = new TranscriptionScheduler(transcribe);
    const commit = scheduler.commit(audio(1));
    const stale = scheduler.preview(audio(2));
    const fresh = scheduler.preview(audio(3));
    await expect(stale).resolves.toBeNull();
    expect(calls.map((call) => call.audio[0])).toEqual([1]);
    calls[0].resolve("committed");
    await commit;
    await settled();
    expect(calls.map((call) => call.audio[0])).toEqual([1, 3]);
    calls[1].resolve("previewed");
    await expect(fresh).resolves.toEqual({ text: "previewed", words: null });
  });

  it("lets a commit that arrives during a preview run next, and answers a failed preview with null", async () => {
    const { calls, transcribe } = manualTranscriber();
    const scheduler = new TranscriptionScheduler(transcribe);
    const preview = scheduler.preview(audio(1));
    const commit = scheduler.commit(audio(2));
    expect(calls.length).toBe(1);
    calls[0].reject(new Error("decoder hiccup"));
    await expect(preview).resolves.toBeNull();
    await settled();
    expect(calls.map((call) => call.audio[0])).toEqual([1, 2]);
    calls[1].resolve("fine");
    await expect(commit).resolves.toEqual({ text: "fine", words: null });
  });

  it("tries a failed commit twice more in place, then holds the lane with everything behind it", async () => {
    const { calls, transcribe } = manualTranscriber();
    const onHeld = vi.fn();
    const scheduler = new TranscriptionScheduler(transcribe, { onHeld });
    const failing = scheduler.commit(audio(1));
    const behind = scheduler.commit(audio(2));
    let isFailingSettled = false;
    void failing.finally(() => {
      isFailingSettled = true;
    });
    for (let attempt = 0; attempt < 3; attempt += 1) {
      calls[attempt].reject(new Error("engine down"));
      await settled();
    }
    expect(calls.map((call) => call.audio[0])).toEqual([1, 1, 1]);
    expect(scheduler.isHeld).toBe(true);
    expect(onHeld).toHaveBeenCalledWith(expect.objectContaining({ message: "engine down" }));
    expect(scheduler.heldChunks.map((chunk) => chunk[0])).toEqual([1, 2]);
    expect(isFailingSettled).toBe(false);
    // A commit queued while held waits too.
    const later = scheduler.commit(audio(3));
    expect(calls.length).toBe(3);

    scheduler.retryHeld();
    await settled();
    expect(calls.map((call) => call.audio[0])).toEqual([1, 1, 1, 1]);
    calls[3].resolve("one");
    await expect(failing).resolves.toEqual({ text: "one", words: null });
    await settled();
    calls[4].resolve("two");
    await expect(behind).resolves.toEqual({ text: "two", words: null });
    await settled();
    calls[5].resolve("three");
    await expect(later).resolves.toEqual({ text: "three", words: null });
    expect(scheduler.isHeld).toBe(false);
  });

  it("rejects every held commit when the user discards them, and runs again for what comes next", async () => {
    const { calls, transcribe } = manualTranscriber();
    const scheduler = new TranscriptionScheduler(transcribe, { commitAttempts: 1 });
    const failing = scheduler.commit(audio(1));
    const behind = scheduler.commit(audio(2));
    calls[0].reject(new Error("engine down"));
    await settled();
    expect(scheduler.isHeld).toBe(true);
    scheduler.discardHeld();
    await expect(failing).rejects.toBeInstanceOf(CommitDiscardedError);
    await expect(behind).rejects.toBeInstanceOf(CommitDiscardedError);
    const next = scheduler.commit(audio(3));
    await settled();
    calls[1].resolve("fresh");
    await expect(next).resolves.toEqual({ text: "fresh", words: null });
  });

  it("drops the queue on clear, the job in flight included, and ignores that job's late answer", async () => {
    const { calls, transcribe } = manualTranscriber();
    const scheduler = new TranscriptionScheduler(transcribe);
    const inFlight = scheduler.commit(audio(1));
    const queued = scheduler.commit(audio(2));
    const preview = scheduler.preview(audio(3));
    scheduler.clear();
    await expect(inFlight).rejects.toBeInstanceOf(CommitDiscardedError);
    await expect(queued).rejects.toBeInstanceOf(CommitDiscardedError);
    await expect(preview).resolves.toBeNull();
    expect(scheduler.pendingCommitCount).toBe(0);
    const next = scheduler.commit(audio(4));
    // The lane is busy until the discarded job answers; its answer goes nowhere and the lane moves on.
    expect(calls.length).toBe(1);
    calls[0].resolve("too late");
    await settled();
    expect(calls.map((call) => call.audio[0])).toEqual([1, 4]);
    calls[1].resolve("next");
    await expect(next).resolves.toEqual({ text: "next", words: null });
  });
});
