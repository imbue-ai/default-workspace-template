/**
 * The one inference lane every transcription of a page goes through (the workspace-voice-input spec, D3): one job
 * at a time, commits in audio order ahead of any preview, and only the newest preview, parked until no commit is
 * waiting. Committed words are the product and previews are decoration, so a preview never delays a commit and a
 * burst of previews never queues up.
 *
 * A commit that fails is tried again in place, twice; one that still fails holds the lane: its audio and every
 * commit behind it wait, in order, for the user's Retry or Discard (the spec's fix 5), and their promises wait
 * with them rather than settling out of order.
 */

import type { TranscriptionResult } from "./speechEngine";

export type Transcribe = (audio: Float32Array) => Promise<TranscriptionResult>;

/** A held commit the user discarded: its words are gone, by choice. */
export class CommitDiscardedError extends Error {
  constructor() {
    super("The dictation's unfinished words were discarded");
    this.name = "CommitDiscardedError";
  }
}

interface CommitJob {
  readonly kind: "commit";
  readonly audio: Float32Array;
  attempts: number;
  resolve(result: TranscriptionResult): void;
  reject(error: Error): void;
}

interface PreviewJob {
  readonly kind: "preview";
  readonly audio: Float32Array;
  resolve(result: TranscriptionResult | null): void;
}

export interface SchedulerOptions {
  /** How many times a commit is run before the lane holds; the spec's two retries make three. */
  commitAttempts?: number;
  /** Called when a commit has failed for good and the lane holds. */
  onHeld?: (error: Error) => void;
}

export class TranscriptionScheduler {
  private readonly transcribe: Transcribe;
  private readonly commitAttempts: number;
  private readonly onHeld: (error: Error) => void;
  private readonly commits: CommitJob[] = [];
  private parkedPreview: PreviewJob | null = null;
  private isRunning = false;
  private isHeldOnFailure = false;

  constructor(transcribe: Transcribe, options: SchedulerOptions = {}) {
    this.transcribe = transcribe;
    this.commitAttempts = options.commitAttempts ?? 3;
    this.onHeld = options.onHeld ?? (() => undefined);
  }

  /** Queue audio whose words are final: runs after every commit before it, and resolves in that order. */
  commit(audio: Float32Array): Promise<TranscriptionResult> {
    return new Promise<TranscriptionResult>((resolve, reject) => {
      this.commits.push({ kind: "commit", audio, attempts: 0, resolve, reject });
      this.pump();
    });
  }

  /** Park audio for a preview; a newer preview replaces one that has not run yet, which then answers null, as
   *  does a preview that fails. */
  preview(audio: Float32Array): Promise<TranscriptionResult | null> {
    return new Promise<TranscriptionResult | null>((resolve) => {
      this.parkedPreview?.resolve(null);
      this.parkedPreview = { kind: "preview", audio, resolve };
      this.pump();
    });
  }

  get pendingCommitCount(): number {
    return this.commits.length;
  }

  /** Whether a commit has failed for good and the lane waits for Retry or Discard. */
  get isHeld(): boolean {
    return this.isHeldOnFailure;
  }

  /** The held audio, the failed commit first, for the notice to say how much is waiting. */
  get heldChunks(): readonly Float32Array[] {
    return this.isHeldOnFailure ? this.commits.map((job) => job.audio) : [];
  }

  /** Run the held commits again, in order, from the one that failed. */
  retryHeld(): void {
    if (!this.isHeldOnFailure) return;
    this.isHeldOnFailure = false;
    if (this.commits.length > 0) this.commits[0].attempts = 0;
    this.pump();
  }

  /** Give the held commits up: each rejects, in order, and the lane runs again for what comes next. */
  discardHeld(): void {
    if (!this.isHeldOnFailure) return;
    this.isHeldOnFailure = false;
    this.rejectQueuedCommits();
    this.pump();
  }

  /** Drop everything queued (the user discarded the unfinished words): queued commits reject, the parked preview
   *  answers null, and a job in flight is the caller's to ignore. */
  clear(): void {
    this.isHeldOnFailure = false;
    this.rejectQueuedCommits();
    this.parkedPreview?.resolve(null);
    this.parkedPreview = null;
  }

  private rejectQueuedCommits(): void {
    const discarded = this.commits.splice(0);
    for (const job of discarded) job.reject(new CommitDiscardedError());
  }

  private pump(): void {
    if (this.isRunning || this.isHeldOnFailure) return;
    const commit = this.commits[0];
    if (commit !== undefined) {
      this.runCommit(commit);
      return;
    }
    const preview = this.parkedPreview;
    if (preview !== null) {
      this.parkedPreview = null;
      this.runPreview(preview);
    }
  }

  private runCommit(job: CommitJob): void {
    this.isRunning = true;
    job.attempts += 1;
    this.transcribe(job.audio).then(
      (result) => {
        this.isRunning = false;
        // A clear while this ran took the job out of the queue; its result is nobody's.
        if (this.commits[0] === job) {
          this.commits.shift();
          job.resolve(result);
        }
        this.pump();
      },
      (error: unknown) => {
        this.isRunning = false;
        if (this.commits[0] === job && job.attempts >= this.commitAttempts) {
          this.isHeldOnFailure = true;
          this.onHeld(error instanceof Error ? error : new Error(String(error)));
        }
        this.pump();
      },
    );
  }

  private runPreview(job: PreviewJob): void {
    this.isRunning = true;
    this.transcribe(job.audio).then(
      (result) => {
        this.isRunning = false;
        job.resolve(result);
        this.pump();
      },
      () => {
        this.isRunning = false;
        job.resolve(null);
        this.pump();
      },
    );
  }
}
