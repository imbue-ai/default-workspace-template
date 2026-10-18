/** Dropping files onto the viewer (docs/system/specs/screen/spec.md section 4.3): the service's ``/api/drop`` writes
 *  them into the Screen's desktop folder, where xfdesktop shows them as icons, and answers where they landed. The
 *  controlling session alone may drop (the lease arbitrates, so a visitor in control may too). */

import { describeRefusal } from "./service_client";

/** The service's cap on one drop, every file together; checked here first so an oversize drop needs no round trip. */
export const DROP_MAX_BYTES = 64 * 1024 * 1024;

export interface DropAnswer {
  /** The folder the files were written into. */
  directory: string;
  /** The names they were saved under, in the order sent (a clash takes a numeric suffix). */
  saved: string[];
}

export type DropOutcome = { landed: DropAnswer } | { refused: string };

export async function uploadDroppedFiles(files: File[], sessionId: string): Promise<DropOutcome> {
  if (files.length === 0) return { refused: "nothing to drop" };
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
  if (totalBytes > DROP_MAX_BYTES)
    return { refused: `the files are over the ${DROP_MAX_BYTES / (1024 * 1024)} MiB cap` };
  const form = new FormData();
  form.set("session_id", sessionId);
  for (const file of files) form.append("files", file, file.name);
  const response = await fetch("/api/drop", { method: "POST", body: form });
  if (!response.ok) return { refused: await describeRefusal(response) };
  return { landed: (await response.json()) as DropAnswer };
}

/** What the strip says about a drop: where the files landed, or why they did not. */
export function describeDropOutcome(outcome: DropOutcome): string {
  if ("refused" in outcome) return `The drop failed: ${outcome.refused}`;
  const { directory, saved } = outcome.landed;
  if (saved.length === 1) return `Saved to ${directory}: ${saved[0]}`;
  return `Saved ${saved.length} files to ${directory}`;
}
