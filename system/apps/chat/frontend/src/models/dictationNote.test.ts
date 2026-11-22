import { beforeEach, describe, expect, it } from "vitest";
import { installLocalStoragePolyfill } from "@imbue/workspace-ui/src/testing/localStorage";

// The drafts' marks live in localStorage, which the node test env lacks.
installLocalStoragePolyfill();

import { buildMessageWithAttachments, parseMessageAttachments } from "./attachments";
import {
  DICTATION_NOTE,
  forgetDictatedSpeech,
  hasDictatedSpeech,
  markDictatedSpeech,
  withDictationNote,
  withoutDictationNotes,
} from "./dictationNote";

const IMAGE_PATH = "/code/uploads/aaa/diagram.png";
const FILE_PATH = "/code/uploads/bbb/notes.txt";

describe("DICTATION_NOTE", () => {
  it("is wrapped in the tag pair every reader recognises it by, on lines of their own", () => {
    expect(DICTATION_NOTE.startsWith("<dictation-note>\n")).toBe(true);
    expect(DICTATION_NOTE.endsWith("\n</dictation-note>")).toBe(true);
    expect(DICTATION_NOTE.length).toBeGreaterThan("<dictation-note>\n\n</dictation-note>".length);
  });
});

describe("withDictationNote", () => {
  it("puts the note after the text, set off by a blank line", () => {
    expect(withDictationNote("rename the parser module")).toBe(`rename the parser module\n\n${DICTATION_NOTE}`);
  });

  it("leaves a message with no words as it is", () => {
    expect(withDictationNote("")).toBe("");
    expect(withDictationNote("  \n\t ")).toBe("  \n\t ");
  });

  it("leaves a command as it is, since a harness reads one only when it is the whole message", () => {
    expect(withDictationNote("/compact")).toBe("/compact");
    expect(withDictationNote("/model sonnet")).toBe("/model sonnet");
    expect(withDictationNote("!ls -la")).toBe("!ls -la");
    expect(withDictationNote("  /compact\n")).toBe("  /compact\n");
  });

  it("still notes a message that merely mentions a path or an exclamation", () => {
    expect(withDictationNote("open /tmp/report.txt now!")).toBe(`open /tmp/report.txt now!\n\n${DICTATION_NOTE}`);
  });
});

describe("withoutDictationNotes", () => {
  it("gives back exactly the text the note was added to", () => {
    for (const text of ["rename the parser module", "two\n\nparagraphs", "ends in a newline\n", "  padded  "]) {
      expect(withoutDictationNotes(withDictationNote(text))).toEqual({ text, hadNote: true });
    }
  });

  it("reports no note in ordinary text, and returns it untouched", () => {
    const text = "nothing dictated here\n\nnot even in <angle brackets>";
    expect(withoutDictationNotes(text)).toEqual({ text, hadNote: false });
    expect(withoutDictationNotes("")).toEqual({ text: "", hadNote: false });
  });

  it("recognises a note by its tags, whatever it says", () => {
    const reworded = "<dictation-note>\nAnother wording.\nOver two lines.\n</dictation-note>";
    expect(withoutDictationNotes(`ship it\n\n${reworded}`)).toEqual({ text: "ship it", hadNote: true });
  });

  it("removes every note from a block that joins several messages, and nothing between them", () => {
    const block = [withDictationNote("first, dictated"), "second, typed", withDictationNote("third, dictated")].join(
      "\n",
    );
    expect(withoutDictationNotes(block)).toEqual({
      text: "first, dictated\nsecond, typed\nthird, dictated",
      hadNote: true,
    });
  });

  it("leaves nothing of a note that stands alone", () => {
    expect(withoutDictationNotes(DICTATION_NOTE)).toEqual({ text: "", hadNote: true });
  });
});

describe("a note in a message with attachments", () => {
  it("sits before the attachment block, which still parses, and comes off the words before it", () => {
    const typed = "look at these";
    const content = buildMessageWithAttachments(withDictationNote(typed), [IMAGE_PATH, FILE_PATH]);

    const parsed = parseMessageAttachments(content);

    expect(parsed.attachmentBlock).toBe(`![${IMAGE_PATH}](${IMAGE_PATH})\n[${FILE_PATH}](${FILE_PATH})`);
    expect(parsed.visibleText).toBe(`${typed}\n\n${DICTATION_NOTE}`);
    expect(withoutDictationNotes(parsed.visibleText)).toEqual({ text: typed, hadNote: true });
  });

  it("leaves the attachment block in place when the notes are taken off the whole message", () => {
    const content = buildMessageWithAttachments(withDictationNote("look at this"), [IMAGE_PATH]);

    expect(withoutDictationNotes(content).text).toBe(buildMessageWithAttachments("look at this", [IMAGE_PATH]));
  });
});

describe("the drafts that may hold dictated speech", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("marks one chat's draft and not another's", () => {
    expect(hasDictatedSpeech("agent-1")).toBe(false);

    markDictatedSpeech("agent-1");

    expect(hasDictatedSpeech("agent-1")).toBe(true);
    expect(hasDictatedSpeech("agent-2")).toBe(false);
    expect(localStorage.getItem("message-dictated:agent-1")).not.toBeNull();
  });

  it("forgets one chat's mark and leaves another's", () => {
    markDictatedSpeech("agent-1");
    markDictatedSpeech("agent-2");

    forgetDictatedSpeech("agent-1");

    expect(hasDictatedSpeech("agent-1")).toBe(false);
    expect(hasDictatedSpeech("agent-2")).toBe(true);
    expect(localStorage.getItem("message-dictated:agent-1")).toBeNull();
  });

  it("stays marked however often it is marked, and takes forgetting an unmarked draft", () => {
    markDictatedSpeech("agent-1");
    markDictatedSpeech("agent-1");
    expect(hasDictatedSpeech("agent-1")).toBe(true);

    forgetDictatedSpeech("agent-2");
    expect(hasDictatedSpeech("agent-2")).toBe(false);
  });
});
