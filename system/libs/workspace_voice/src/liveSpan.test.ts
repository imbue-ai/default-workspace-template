import { describe, expect, it } from "vitest";
import { appendFinal, clearTentative, fitWords, openSpan, renderSpan, setTentative } from "./liveSpan";

describe("the live span", () => {
  it("appends at the end of a draft with one space, as typing would", () => {
    let span = openSpan("Hello", 5, 5);
    span = setTentative(span, "wor");
    expect(renderSpan(span)).toEqual({ text: "Hello wor", draft: "Hello", spanStart: 6, spanEnd: 9 });
    // "Hello" is an unfinished sentence, so the words continue it rather than starting their own.
    span = appendFinal(clearTentative(span), "World.");
    expect(renderSpan(span)).toEqual({ text: "Hello world.", draft: "Hello world.", spanStart: 6, spanEnd: 12 });
  });

  it("leaves an empty box's words alone", () => {
    const span = appendFinal(openSpan("", 0, 0), "Rerun the two flakes.");
    expect(renderSpan(span).text).toBe("Rerun the two flakes.");
    expect(renderSpan(span).draft).toBe("Rerun the two flakes.");
  });

  it("fits words into the middle of a sentence: a lowered first letter, no final stop, spaces either side", () => {
    const text = "I went to the yesterday.";
    const span = appendFinal(openSpan(text, 13, 13), "Big store.");
    expect(renderSpan(span)).toEqual({
      text: "I went to the big store yesterday.",
      draft: "I went to the big store yesterday.",
      spanStart: 14,
      spanEnd: 23,
    });
  });

  it("keeps a capital after a finished sentence, on I, and on an acronym", () => {
    expect(fitWords("Then we left.", "It rained.", "").text).toBe("Then we left.");
    expect(fitWords("I think so.", "and then", "").text).toBe("I think so.");
    expect(fitWords("I'm sure.", "and then", "").text).toBe("I'm sure.");
    expect(fitWords("CI is red.", "and then", "").text).toBe("CI is red.");
    expect(fitWords("Nothing happened.", "and then", "").text).toBe("nothing happened.");
  });

  it("joins punctuation on the right without a space and keeps a question mark", () => {
    expect(fitWords("Is it done?", "", ", she asked.")).toEqual({ leading: "", text: "Is it done?", trailing: "" });
    expect(fitWords("It is done.", "", ". Next")).toEqual({ leading: "", text: "It is done", trailing: "" });
    expect(fitWords("ship it.", "(", ")")).toEqual({ leading: "", text: "ship it.", trailing: "" });
  });

  it("replaces a selection only once a word arrives, so a silent dictation leaves the text as it was", () => {
    const span = openSpan("Keep this and drop that.", 14, 23);
    expect(renderSpan(span)).toEqual({
      text: "Keep this and drop that.",
      draft: "Keep this and drop that.",
      spanStart: 14,
      spanEnd: 23,
    });
    const heard = setTentative(span, "Change");
    expect(renderSpan(heard)).toEqual({
      text: "Keep this and change.",
      draft: "Keep this and .",
      spanStart: 14,
      spanEnd: 20,
    });
  });

  it("saves only the final words to the draft while the tail changes", () => {
    let span = appendFinal(openSpan("", 0, 0), "First sentence.");
    span = setTentative(span, "and now");
    expect(renderSpan(span)).toEqual({
      text: "First sentence. and now",
      draft: "First sentence.",
      spanStart: 0,
      spanEnd: 23,
    });
    span = clearTentative(span);
    expect(renderSpan(span).text).toBe("First sentence.");
  });

  it("reads the selection whichever way it was dragged, clamped to the text", () => {
    const span = openSpan("abc", 3, 1);
    expect(span.before).toBe("a");
    expect(span.selected).toBe("bc");
    expect(span.after).toBe("");
    expect(openSpan("abc", 10, 12).before).toBe("abc");
  });
});
