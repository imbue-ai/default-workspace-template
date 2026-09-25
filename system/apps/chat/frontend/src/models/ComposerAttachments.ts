/**
 * Per-agent store of the attachments the user has staged on the current draft.
 *
 * Files are uploaded to the agent VM as soon as they are dropped, pasted, or
 * picked, so each item moves from "uploading" to "ready" (or "error"). Held in a
 * model module rather than a view closure so both the composer (the attach
 * button / chips) and the chat panel (its panel-wide drop target) can stage the
 * same agent's attachments.
 *
 * The ready items are also persisted to localStorage beside the draft text, so a
 * chip survives a reload the way the text does, and a document of the same origin
 * that staged one (the chat root, drafting into a page not yet loaded) is seen by
 * the page once it loads: a ``storage`` event brings the stored list in.
 */

import m from "mithril";
import { deleteAttachment } from "./attachments";
import { isImagePath } from "./attachments";
import { uploadAttachment } from "./attachments";
import type { UploadedAttachment } from "./attachments";
import { describeRequestError } from "@imbue/workspace-ui/src/models/request-error";

export type ComposerAttachmentStatus = "uploading" | "ready" | "error";

export interface ComposerAttachment {
  /** Stable id for keying the rendered chip and addressing removals. */
  localId: string;
  /** Original filename, shown while uploading and on the chip. */
  fileName: string;
  /** Whether this is an image (drives thumbnail vs file chip), known up front
   *  from the dropped File so the chip can choose its shape before upload ends. */
  isImage: boolean;
  status: ComposerAttachmentStatus;
  /** One line saying what the file stands for, when it is not the user's own file: an element
   *  reference's chip shows it in place of the size, and as its tooltip. */
  summary?: string;
  /** Present once the upload succeeds. */
  uploaded?: UploadedAttachment;
  /** Present when the upload failed. */
  error?: string;
  /** Resolves when the upload settles (ready or error); awaited at send time so
   *  an in-flight upload is included rather than dropped. */
  uploadSettled?: Promise<void>;
}

const STORAGE_KEY_PREFIX = "composer-attachments:";
const LOCAL_ID_PREFIX = "composer-att-";

function storageKey(chatId: string): string {
  return `${STORAGE_KEY_PREFIX}${chatId}`;
}

let _nextLocalId = 0;
const _attachmentsByChat: Record<string, ComposerAttachment[]> = {};

/** What a ready attachment persists as: the fields a chip and a send need, none of the in-flight ones. */
type StoredAttachment = Pick<ComposerAttachment, "localId" | "fileName" | "isImage" | "summary"> & {
  uploaded: UploadedAttachment;
};

function _storedAttachmentsOf(chatId: string): StoredAttachment[] {
  let raw: string | null;
  try {
    raw = localStorage.getItem(storageKey(chatId));
  } catch {
    return [];
  }
  if (raw === null) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as StoredAttachment[]) : [];
  } catch {
    return [];
  }
}

function _persist(chatId: string, attachments: readonly ComposerAttachment[]): void {
  const stored: StoredAttachment[] = [];
  for (const attachment of attachments) {
    if (attachment.status !== "ready" || attachment.uploaded === undefined) continue;
    const { localId, fileName, isImage, summary, uploaded } = attachment;
    stored.push({ localId, fileName, isImage, summary, uploaded });
  }
  try {
    if (stored.length === 0) {
      localStorage.removeItem(storageKey(chatId));
    } else {
      localStorage.setItem(storageKey(chatId), JSON.stringify(stored));
    }
  } catch {
    // Storage may be full or refused; the in-memory list still serves this document.
  }
}

/** Keep minted ids clear of the ones a stored list brought back from an earlier document. */
function _reserveLocalIds(attachments: readonly StoredAttachment[]): void {
  for (const attachment of attachments) {
    const suffix = Number(attachment.localId.slice(LOCAL_ID_PREFIX.length));
    if (Number.isInteger(suffix) && suffix >= _nextLocalId) _nextLocalId = suffix + 1;
  }
}

/** The stored list of ``chatId`` as ready attachments, adopted once into memory (the first read after a load). */
function _hydrate(chatId: string): ComposerAttachment[] {
  const stored = _storedAttachmentsOf(chatId);
  _reserveLocalIds(stored);
  return stored.map((attachment) => ({ ...attachment, status: "ready" as const }));
}

export function getComposerAttachments(chatId: string): ComposerAttachment[] {
  const held = _attachmentsByChat[chatId];
  if (held !== undefined) return held;
  const hydrated = _hydrate(chatId);
  _attachmentsByChat[chatId] = hydrated;
  return hydrated;
}

function _setAttachments(chatId: string, attachments: ComposerAttachment[]): void {
  _attachmentsByChat[chatId] = attachments;
  _persist(chatId, attachments);
}

function _patchAttachment(chatId: string, localId: string, patch: Partial<ComposerAttachment>): void {
  const list = _attachmentsByChat[chatId];
  if (list === undefined) {
    return;
  }
  _setAttachments(
    chatId,
    list.map((item) => (item.localId === localId ? { ...item, ...patch } : item)),
  );
  m.redraw();
}

/**
 * Another document of this origin wrote ``chatId``'s stored list (the root staging a reference for a page it
 * had not loaded yet): take its ready items in, keeping whatever this document has in flight.
 */
function _takeStoredChanges(chatId: string): void {
  const held = _attachmentsByChat[chatId];
  if (held === undefined) return;
  const stored = _hydrate(chatId);
  const storedIds = new Set(stored.map((attachment) => attachment.localId));
  const inFlight = held.filter((attachment) => attachment.status !== "ready" && !storedIds.has(attachment.localId));
  _attachmentsByChat[chatId] = [...stored, ...inFlight];
  m.redraw();
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (event: StorageEvent) => {
    if (event.key === null || !event.key.startsWith(STORAGE_KEY_PREFIX)) return;
    _takeStoredChanges(event.key.slice(STORAGE_KEY_PREFIX.length));
  });
}

function _startUpload(chatId: string, file: File, summary: string | undefined): void {
  const localId = `${LOCAL_ID_PREFIX}${_nextLocalId++}`;
  const item: ComposerAttachment = {
    localId,
    fileName: file.name,
    isImage: file.type.startsWith("image/") || isImagePath(file.name),
    status: "uploading",
    summary,
  };
  _setAttachments(chatId, [...getComposerAttachments(chatId), item]);
  item.uploadSettled = uploadAttachment(file)
    .then((uploaded) => {
      _patchAttachment(chatId, localId, { status: "ready", uploaded });
    })
    .catch((error: unknown) => {
      _patchAttachment(chatId, localId, { status: "error", error: describeRequestError(error) });
    });
}

/**
 * Upload each dropped / pasted / picked file to the agent VM, staging it as a
 * composer attachment. Returns immediately; status transitions drive redraws.
 */
export function uploadFilesToComposer(chatId: string, files: FileList | readonly File[] | null | undefined): void {
  if (!files) {
    return;
  }
  const fileArray = Array.from(files);
  if (fileArray.length === 0) {
    return;
  }
  for (const file of fileArray) {
    _startUpload(chatId, file, undefined);
  }
  m.redraw();
}

/** Upload a file the chat made for the user (an element reference), whose chip says ``summary`` instead of a size. */
export function uploadDescribedFileToComposer(chatId: string, file: File, summary: string): void {
  _startUpload(chatId, file, summary);
  m.redraw();
}

/**
 * Remove an attachment from the composer, deleting its VM copy so a removed file
 * does not linger on the agent (removal is treated as revoking access).
 */
export function removeComposerAttachment(chatId: string, localId: string): void {
  const list = getComposerAttachments(chatId);
  const item = list.find((attachment) => attachment.localId === localId);
  _setAttachments(
    chatId,
    list.filter((attachment) => attachment.localId !== localId),
  );
  m.redraw();
  if (item?.uploaded) {
    void deleteAttachment(item.uploaded.path);
  }
}

/** Await any in-flight uploads so their paths are available before sending. */
export async function waitForComposerUploads(chatId: string): Promise<void> {
  const pending = getComposerAttachments(chatId)
    .filter((attachment) => attachment.status === "uploading" && attachment.uploadSettled)
    .map((attachment) => attachment.uploadSettled as Promise<void>);
  if (pending.length > 0) {
    await Promise.allSettled(pending);
  }
}

export function getReadyAttachmentPaths(chatId: string): string[] {
  return getComposerAttachments(chatId)
    .filter((attachment) => attachment.status === "ready" && attachment.uploaded)
    .map((attachment) => (attachment.uploaded as UploadedAttachment).path);
}

export function hasReadyAttachments(chatId: string): boolean {
  return getComposerAttachments(chatId).some((attachment) => attachment.status === "ready");
}

/**
 * Clear the composer's attachments WITHOUT deleting the VM copies -- used after a
 * successful send, where the files are now referenced by the sent message.
 */
export function clearComposerAttachments(chatId: string): void {
  _setAttachments(chatId, []);
  m.redraw();
}

/** Restore a snapshot of attachments (used to roll back a failed send). */
export function restoreComposerAttachments(chatId: string, attachments: readonly ComposerAttachment[]): void {
  _setAttachments(chatId, [...attachments]);
  m.redraw();
}
