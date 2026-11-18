import { describe, expect, it } from "vitest";
import {
  isLongPaste,
  isPastedTextFileName,
  lineCountOf,
  nextPastedTextFileName,
  PASTED_TEXT_MIN_CHARACTERS,
  PASTED_TEXT_MIN_LINES,
  pastedTextFileOf,
  pastedTextSummaryOf,
} from "./pastedText";

describe("lineCountOf", () => {
  it("counts lines, a final newline ending the last one", () => {
    expect(lineCountOf("")).toBe(0);
    expect(lineCountOf("one")).toBe(1);
    expect(lineCountOf("one\n")).toBe(1);
    expect(lineCountOf("one\ntwo")).toBe(2);
    expect(lineCountOf("one\n\nthree\n")).toBe(3);
  });
});

describe("isLongPaste", () => {
  it("keeps a paste under both limits as text", () => {
    expect(isLongPaste("a".repeat(PASTED_TEXT_MIN_CHARACTERS - 1))).toBe(false);
    expect(isLongPaste("line\n".repeat(PASTED_TEXT_MIN_LINES - 1))).toBe(false);
  });

  it("takes a paste at the character limit as an attachment", () => {
    expect(isLongPaste("a".repeat(PASTED_TEXT_MIN_CHARACTERS))).toBe(true);
  });

  it("takes a paste of many short lines as an attachment", () => {
    expect(isLongPaste("x\n".repeat(PASTED_TEXT_MIN_LINES))).toBe(true);
  });
});

describe("pastedTextSummaryOf", () => {
  it("says how many lines the paste holds", () => {
    expect(pastedTextSummaryOf("only")).toBe("Pasted text, 1 line");
    expect(pastedTextSummaryOf("a\nb\nc\n")).toBe("Pasted text, 3 lines");
    expect(pastedTextSummaryOf("x\n".repeat(1200))).toBe("Pasted text, 1,200 lines");
  });
});

describe("nextPastedTextFileName", () => {
  it("numbers each paste of a draft after the highest one staged", () => {
    expect(nextPastedTextFileName([])).toBe("pasted-text-1.txt");
    expect(nextPastedTextFileName(["photo.png", "pasted-text-1.txt"])).toBe("pasted-text-2.txt");
    expect(nextPastedTextFileName(["pasted-text-3.txt", "pasted-text-1.txt"])).toBe("pasted-text-4.txt");
  });

  it("names only its own files as pasted text", () => {
    expect(isPastedTextFileName("pasted-text-12.txt")).toBe(true);
    expect(isPastedTextFileName("pasted-text.txt")).toBe(false);
    expect(isPastedTextFileName("notes.txt")).toBe(false);
  });
});

describe("pastedTextFileOf", () => {
  it("is a plain text file holding the whole paste", async () => {
    const text = "first line\n".repeat(500) + "the last line";
    const file = pastedTextFileOf(text, "pasted-text-1.txt");
    expect(file.name).toBe("pasted-text-1.txt");
    expect(file.type).toBe("text/plain");
    expect(await file.text()).toBe(text);
  });
});
