/**
 * Whether this page is a preview shell: a second shell booted over a copy of the live state
 * to show a proposed change. The backend marks its page with a meta tag and refuses the verbs
 * that would reach the live workspace (an app's stop and start, the update notice's); the
 * store reads this to withhold Stop and Start rather than offer a refusal. Read from the
 * document each time, so a page needs no
 * plumbing to know, and code that runs with no document (a pure test) is never a preview.
 */

export const PREVIEW_SHELL_META_TAG = "system-interface-preview";

export function isPreviewShell(): boolean {
  if (typeof document === "undefined") return false;
  const metaElement = document.querySelector(`meta[name="${PREVIEW_SHELL_META_TAG}"]`);
  return metaElement?.getAttribute("content") === "true";
}
