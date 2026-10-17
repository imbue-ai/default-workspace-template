/**
 * Files going up to the server: what was picked or dropped, flattened to files with their path below the folder
 * they land in, and the queue that sends them a few at a time with progress.
 */

import { uploadFile, type Upload } from "./dufs";

/** A file to send and where it goes below the target folder: `a.png`, or `photos/2024/a.png` from a folder. */
export interface UploadSource {
  file: File;
  relativePath: string;
}

export type UploadStatus = "queued" | "uploading" | "done" | "failed" | "canceled";

export interface UploadItem {
  id: number;
  /** The folder it was dropped into, which refreshes once it lands. */
  folder: string;
  path: string;
  name: string;
  file: File;
  loaded: number;
  status: UploadStatus;
  error: string | null;
}

export interface UploadQueueHost {
  redraw(): void;
  /** A file landed in (somewhere below) `folder`. */
  onUploaded(folder: string): void;
}

/** How many files go up at once. */
const CONCURRENCY = 3;

/** How long a finished batch stays on show, so the check marks are seen before the panel goes. A failure keeps it up. */
export const FINISHED_LINGER_MS = 2500;

export class UploadQueue {
  items: UploadItem[] = [];
  private nextId = 1;
  private readonly running = new Map<number, Upload>();
  private clearTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly host: UploadQueueHost) {}

  add(folder: string, sources: readonly UploadSource[]): void {
    this.cancelClear();
    for (const source of sources) {
      this.items.push({
        id: this.nextId++,
        folder,
        path: `${folder}${source.relativePath}`,
        name: source.relativePath,
        file: source.file,
        loaded: 0,
        status: "queued",
        error: null,
      });
    }
    this.pump();
    this.host.redraw();
  }

  get isBusy(): boolean {
    return this.items.some((item) => item.status === "queued" || item.status === "uploading");
  }

  /** Bytes sent and bytes to send, over everything not canceled. */
  get progress(): { loaded: number; total: number } {
    let loaded = 0;
    let total = 0;
    for (const item of this.items) {
      if (item.status === "canceled") continue;
      total += item.file.size;
      loaded += item.status === "done" ? item.file.size : item.loaded;
    }
    return { loaded, total };
  }

  cancel(id: number): void {
    const item = this.items.find((candidate) => candidate.id === id);
    if (item === undefined) return;
    if (item.status === "queued") item.status = "canceled";
    this.running.get(id)?.abort();
    this.host.redraw();
  }

  cancelAll(): void {
    for (const item of this.items) this.cancel(item.id);
  }

  /** Forget everything that is no longer moving. */
  clearFinished(): void {
    this.cancelClear();
    this.items = this.items.filter((item) => item.status === "queued" || item.status === "uploading");
    this.host.redraw();
  }

  private pump(): void {
    for (const item of this.items) {
      if (this.running.size >= CONCURRENCY) return;
      if (item.status === "queued") this.start(item);
    }
  }

  private start(item: UploadItem): void {
    item.status = "uploading";
    const upload = uploadFile(item.path, item.file, (loaded) => {
      item.loaded = loaded;
      this.host.redraw();
    });
    this.running.set(item.id, upload);
    upload.done
      .then(() => {
        item.status = "done";
        item.loaded = item.file.size;
        this.host.onUploaded(item.folder);
      })
      .catch((error: unknown) => {
        const aborted = item.status === "uploading" && error instanceof Error && error.message === "Canceled";
        item.status = aborted ? "canceled" : "failed";
        item.error = aborted ? null : error instanceof Error ? error.message : String(error);
      })
      .finally(() => {
        this.running.delete(item.id);
        this.pump();
        this.scheduleClear();
        this.host.redraw();
      });
  }

  /** Once everything has gone up cleanly, put the panel away after a moment. */
  private scheduleClear(): void {
    if (this.isBusy || this.items.some((item) => item.status === "failed")) return;
    this.cancelClear();
    this.clearTimer = setTimeout(() => this.clearFinished(), FINISHED_LINGER_MS);
  }

  private cancelClear(): void {
    if (this.clearTimer !== null) clearTimeout(this.clearTimer);
    this.clearTimer = null;
  }
}

/** The files an <input type="file"> picked; a folder pick carries each file's path below the folder picked. */
export function sourcesFromFileList(files: FileList | readonly File[]): UploadSource[] {
  return Array.from(files).map((file) => ({
    file,
    relativePath: file.webkitRelativePath !== "" ? file.webkitRelativePath : file.name,
  }));
}

function readAllEntries(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  // A directory reader hands entries over in batches until it returns an empty one.
  return new Promise((resolve, reject) => {
    const collected: FileSystemEntry[] = [];
    const readBatch = (): void => {
      reader.readEntries((batch) => {
        if (batch.length === 0) {
          resolve(collected);
          return;
        }
        collected.push(...batch);
        readBatch();
      }, reject);
    };
    readBatch();
  });
}

async function sourcesFromEntry(entry: FileSystemEntry, prefix: string): Promise<UploadSource[]> {
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
    return [{ file, relativePath: `${prefix}${entry.name}` }];
  }
  if (!entry.isDirectory) return [];
  const children = await readAllEntries((entry as FileSystemDirectoryEntry).createReader());
  const nested = await Promise.all(children.map((child) => sourcesFromEntry(child, `${prefix}${entry.name}/`)));
  return nested.flat();
}

/** Whether a drag carries files from outside the page, as opposed to entries dragged within it. */
export function isFileDrag(dataTransfer: DataTransfer | null): boolean {
  return dataTransfer !== null && Array.from(dataTransfer.types).includes("Files");
}

/** Everything a drop carries, folders walked into their files. Read synchronously off the event's items, which go
 *  stale once the handler returns. */
export function sourcesFromDrop(dataTransfer: DataTransfer): Promise<UploadSource[]> {
  const entries = Array.from(dataTransfer.items)
    .filter((item) => item.kind === "file")
    .map((item) => item.webkitGetAsEntry?.() ?? null);
  if (entries.length === 0 || entries.some((entry) => entry === null)) {
    return Promise.resolve(sourcesFromFileList(dataTransfer.files));
  }
  return Promise.all(entries.map((entry) => sourcesFromEntry(entry as FileSystemEntry, ""))).then((all) => all.flat());
}
