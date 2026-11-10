/**
 * Whether a keyboard shortcut recorder is listening for a new shortcut. While one is, the shell runs no
 * workspace-wide shortcut pressed in its own document and does not act on an older Imbue Studio's relayed close-tab
 * chord, so a key being recorded does nothing else.
 */

let isRecording = false;

export function setShortcutRecording(value: boolean): void {
  isRecording = value;
}

export function isShortcutRecording(): boolean {
  return isRecording;
}
