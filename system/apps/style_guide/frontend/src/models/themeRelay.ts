/**
 * Hands the theme the shell names this page on to the plain-HTML sample it frames. The page kit in that sample
 * follows `shell:theme` from its parent window, which is this page rather than the shell, so without the relay it
 * would wear only the theme the origin last remembered and miss a preview from Desktop settings.
 */

import { SHELL_THEME } from "@imbue/workspace-ui/src/app_contract";

export interface ThemeMessage {
  readonly theme: string;
  readonly revision: string;
  readonly isPreview: boolean;
}

/** Marks a frame that wants the page's theme. */
export const THEME_RELAY_ATTRIBUTE = "data-theme-relay";

let lastMessage: ThemeMessage | null = null;

function post(frame: HTMLIFrameElement, message: ThemeMessage): void {
  frame.contentWindow?.postMessage({ type: SHELL_THEME, ...message }, window.location.origin);
}

/** Remember the theme the shell named, and send it to every relaying frame on the page. */
export function relayTheme(message: ThemeMessage, doc: Document = document): void {
  lastMessage = message;
  for (const frame of Array.from(doc.querySelectorAll<HTMLIFrameElement>(`iframe[${THEME_RELAY_ATTRIBUTE}]`))) {
    post(frame, message);
  }
}

/** For a frame's `onload`: send it the theme the shell last named, if one has arrived. */
export function relayToLoadedFrame(frame: HTMLIFrameElement): void {
  if (lastMessage !== null) post(frame, lastMessage);
}
