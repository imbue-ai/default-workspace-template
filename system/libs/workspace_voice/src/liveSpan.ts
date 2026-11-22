/**
 * Where dictated words land in a box of text (the workspace-voice-input spec, D4): at the caret, fixed when the
 * dictation starts, with the unfinished words rewritten in place until they are final. Pure: the composer feeds
 * it the box's text and selection and writes back what it renders.
 *
 * The span is the stretch of text the dictation owns: its final words, then its tentative tail. Only the span is
 * ever rewritten, and only the final words reach the saved draft. A selection is replaced when the first word
 * arrives, so a dictation that hears nothing leaves the text as it was. When the caret sits mid-text, the words
 * are fitted to their neighbours: a space on either side where one is missing, the first letter lowered after an
 * unfinished sentence, and the final full stop dropped when the sentence goes on after them.
 */

export interface LiveSpan {
  /** The text before the insertion point, fixed at the start. */
  readonly before: string;
  /** The text after the insertion point (or after the selection), fixed at the start. */
  readonly after: string;
  /** What was selected when the dictation started; kept in the box until the first word arrives. */
  readonly selected: string;
  /** Whether a word has arrived, which is when the selection gives way. */
  readonly hasHeardWords: boolean;
  /** The final words of this dictation so far, each utterance joined to the last with a space. */
  readonly finalText: string;
  /** The unfinished tail of the current utterance. */
  readonly tentativeText: string;
}

export interface RenderedSpan {
  /** What the box shows. */
  readonly text: string;
  /** What is saved: the text without the tentative tail. */
  readonly draft: string;
  /** Where the span's words begin and end in ``text``; the caret belongs at ``spanEnd``. */
  readonly spanStart: number;
  readonly spanEnd: number;
}

/** A span opened at the box's selection. */
export function openSpan(text: string, selectionStart: number, selectionEnd: number): LiveSpan {
  const start = Math.max(0, Math.min(selectionStart, selectionEnd, text.length));
  const end = Math.max(start, Math.min(Math.max(selectionStart, selectionEnd), text.length));
  return {
    before: text.slice(0, start),
    after: text.slice(end),
    selected: text.slice(start, end),
    hasHeardWords: false,
    finalText: "",
    tentativeText: "",
  };
}

function joinWords(first: string, second: string): string {
  return [first, second].filter((part) => part !== "").join(" ");
}

/** Final words arrived (a commit, or the end of an utterance): they join the span's final text. */
export function appendFinal(span: LiveSpan, words: string): LiveSpan {
  const trimmed = words.trim();
  if (trimmed === "") return span;
  return { ...span, hasHeardWords: true, finalText: joinWords(span.finalText, trimmed) };
}

/** The unfinished tail changed: it replaces the last one. */
export function setTentative(span: LiveSpan, words: string): LiveSpan {
  const trimmed = words.trim();
  if (trimmed === "" && !span.hasHeardWords) return span;
  return { ...span, hasHeardWords: span.hasHeardWords || trimmed !== "", tentativeText: trimmed };
}

/** The unfinished tail is gone: the utterance ended (its words came through ``appendFinal``) or was discarded. */
export function clearTentative(span: LiveSpan): LiveSpan {
  return span.tentativeText === "" ? span : { ...span, tentativeText: "" };
}

const SENTENCE_END = /[.!?\n]$/;
const OPENING = /[([{"'“‘\n]$/;
const CLOSING = /^[,.;:!?)\]}"'”’]/;
const LOWERABLE_START = /^[A-Z][a-z]/;
// "I" and its contractions stay capital wherever they land.
const FIRST_PERSON = /^I(?:$|['’]|\s)/;

/** ``words`` fitted between ``before`` and ``after``: the separators they need, and their casing and final stop
 *  adjusted to the sentence they join. */
export function fitWords(
  words: string,
  before: string,
  after: string,
): { leading: string; text: string; trailing: string } {
  let text = words;
  const prev = before.trimEnd();
  const next = after.trimStart();
  const leading = before === "" || /\s$/.test(before) || OPENING.test(before) ? "" : " ";
  if (
    prev !== "" &&
    !SENTENCE_END.test(prev) &&
    !OPENING.test(prev) &&
    LOWERABLE_START.test(text) &&
    !FIRST_PERSON.test(text)
  ) {
    text = text[0].toLowerCase() + text.slice(1);
  }
  let trailing = "";
  if (next !== "") {
    if (/^[a-z0-9]/.test(next) && text.endsWith(".")) text = text.slice(0, -1);
    if (CLOSING.test(next) && text.endsWith(".") && next.startsWith(".")) text = text.slice(0, -1);
    trailing = /^\s/.test(after) || CLOSING.test(next) ? "" : " ";
  }
  return { leading, text, trailing };
}

/** The box's text and the saved draft for ``span``, with where the span sits. */
export function renderSpan(span: LiveSpan): RenderedSpan {
  if (!span.hasHeardWords) {
    const text = span.before + span.selected + span.after;
    return { text, draft: text, spanStart: span.before.length, spanEnd: span.before.length + span.selected.length };
  }
  const shown = fitWords(joinWords(span.finalText, span.tentativeText), span.before, span.after);
  const spanStart = span.before.length + shown.leading.length;
  const text = span.before + shown.leading + shown.text + shown.trailing + span.after;
  let draft: string;
  if (span.finalText === "") {
    draft = span.before + span.after;
  } else {
    const saved = fitWords(span.finalText, span.before, span.after);
    draft = span.before + saved.leading + saved.text + saved.trailing + span.after;
  }
  return { text, draft, spanStart, spanEnd: spanStart + shown.text.length };
}
