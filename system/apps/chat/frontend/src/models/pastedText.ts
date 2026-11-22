/**
 * A long paste enters the composer as an attachment rather than as text: a ``pasted-text-<n>.txt`` file
 * uploaded through the ordinary attachment path, so the draft stays short enough to read and edit, and the
 * agent opens the whole text from the file the sent message names, whatever its harness.
 */

/** A paste at least this many characters long becomes an attachment. */
export const PASTED_TEXT_MIN_CHARACTERS = 4000;

/** A paste of at least this many lines becomes an attachment, however short its lines. */
export const PASTED_TEXT_MIN_LINES = 80;

const PASTED_TEXT_FILE_NAME_RE = /^pasted-text-(\d+)\.txt$/;

/** How many lines ``text`` has, a final newline ending the last line rather than starting another. */
export function lineCountOf(text: string): number {
  if (text === "") return 0;
  const body = text.endsWith("\n") ? text.slice(0, -1) : text;
  return body.split("\n").length;
}

/** Whether a paste of ``text`` becomes an attachment. */
export function isLongPaste(text: string): boolean {
  return text.length >= PASTED_TEXT_MIN_CHARACTERS || lineCountOf(text) >= PASTED_TEXT_MIN_LINES;
}

/** What a pasted-text chip says in place of a size. */
export function pastedTextSummaryOf(text: string): string {
  const lines = lineCountOf(text);
  return lines === 1 ? "Pasted text, 1 line" : `Pasted text, ${lines.toLocaleString("en-US")} lines`;
}

/** Whether ``fileName`` names a pasted-text attachment. */
export function isPastedTextFileName(fileName: string): boolean {
  return PASTED_TEXT_FILE_NAME_RE.test(fileName);
}

/** The next free ``pasted-text-<n>.txt`` among the draft's ``stagedFileNames``, so two pastes in one message
 *  can be told apart by name. */
export function nextPastedTextFileName(stagedFileNames: readonly string[]): string {
  let highest = 0;
  for (const fileName of stagedFileNames) {
    const match = fileName.match(PASTED_TEXT_FILE_NAME_RE);
    if (match !== null) highest = Math.max(highest, Number(match[1]));
  }
  return `pasted-text-${highest + 1}.txt`;
}

/** The file a long paste is attached as. */
export function pastedTextFileOf(text: string, fileName: string): File {
  return new File([text], fileName, { type: "text/plain" });
}
