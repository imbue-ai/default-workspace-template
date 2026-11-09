/**
 * Whether a keyboard shortcut recorder is listening for a new shortcut. While one is, the keys Imbue Studio
 * relays into the workspace (the close-window chord) belong to the recorder, so the shell does not act on them.
 */

let isRecording = false;

export function setShortcutRecording(value: boolean): void {
  isRecording = value;
}

export function isShortcutRecording(): boolean {
  return isRecording;
}
