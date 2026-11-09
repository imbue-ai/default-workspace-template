/**
 * Whether a keyboard shortcut recorder is listening for a new shortcut. While one is, the shell does not act on
 * the close-window chord Imbue Studio relays into the workspace, so recording closes no window.
 */

let isRecording = false;

export function setShortcutRecording(value: boolean): void {
  isRecording = value;
}

export function isShortcutRecording(): boolean {
  return isRecording;
}
