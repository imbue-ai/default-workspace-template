/**
 * The dictation note: what a sent message tells the model when part of it may have been dictated, and
 * which drafts may hold dictated words.
 *
 * Speech recognition mishears, so a message sent from a draft that dictation wrote into ends with a short
 * note asking the model to read the words leniently. The note is for the model alone: it rides in the text
 * the agent receives and the transcript records, inside a tag pair, and every bubble that shows a person's
 * message, and the composer when a message comes back to it, takes it out by that pair, whatever the note
 * says. The backend does the same for its notification bubbles and its chat-naming prompt
 * (``imbue/chat/dictation_note.py``).
 *
 * Whether a draft may hold dictated words is kept per chat in localStorage, beside the draft itself and
 * nowhere else, so the composer and the views that reach a draft from outside it read one answer.
 */

/** What a message that may hold dictated words ends with. */
export const DICTATION_NOTE =
  "<dictation-note>\n" +
  "Part of this message was dictated aloud and transcribed by speech recognition, so it may contain misheard " +
  "words, homophones, and missing or misplaced punctuation; names, file paths, and code identifiers are the " +
  "likeliest to be wrong. Read it for what the speaker most plausibly meant in context rather than literally, " +
  "and ask before acting if a possible mishearing would change what you do.\n" +
  "</dictation-note>";

// A note with the blank line that sets it off (the backend's ``_DICTATION_NOTE_RE`` in
// imbue/chat/dictation_note.py reads the same -- keep the two in step). Non-greedy, since a block that
// joins several messages (a queue handed back to the composer) carries one note per dictated message.
const DICTATION_NOTE_RE = /(?:\n\n)?<dictation-note>[\s\S]*?<\/dictation-note>/g;

const DICTATED_SPEECH_KEY_PREFIX = "message-dictated:";

/**
 * ``text`` as it is sent when it may hold dictated words: the text, a blank line, the note. A message with
 * no words gets none, and neither does a slash command or bash-mode input: a harness runs one only when it
 * is the whole message, and the backend reads ``/compact`` the same way.
 */
export function withDictationNote(text: string): string {
  const trimmed = text.trim();
  if (trimmed === "" || trimmed.startsWith("/") || trimmed.startsWith("!")) {
    return text;
  }
  return `${text}\n\n${DICTATION_NOTE}`;
}

/**
 * ``text`` without any dictation note, each taken out with the blank line that set it off, so a text
 * ``withDictationNote`` added a note to comes back as it was; ``hadNote`` says whether there was one.
 */
export function withoutDictationNotes(text: string): { text: string; hadNote: boolean } {
  const stripped = text.replace(DICTATION_NOTE_RE, "");
  return { text: stripped, hadNote: stripped !== text };
}

function dictatedSpeechKey(chatId: string): string {
  return `${DICTATED_SPEECH_KEY_PREFIX}${chatId}`;
}

/** Whether ``chatId``'s draft may hold dictated words. */
export function hasDictatedSpeech(chatId: string): boolean {
  return localStorage.getItem(dictatedSpeechKey(chatId)) !== null;
}

/** Record that dictated words went into ``chatId``'s draft. */
export function markDictatedSpeech(chatId: string): void {
  localStorage.setItem(dictatedSpeechKey(chatId), "1");
}

/** Record that ``chatId``'s draft holds no dictated words any more: it was sent, or emptied. */
export function forgetDictatedSpeech(chatId: string): void {
  localStorage.removeItem(dictatedSpeechKey(chatId));
}
