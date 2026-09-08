/**
 * Marks the test page as a preview shell the way the backend does, and returns the undo.
 */

import { PREVIEW_SHELL_META_TAG } from "../models/PreviewShell";

export function markPageAsPreviewShell(): () => void {
  const metaElement = document.createElement("meta");
  metaElement.setAttribute("name", PREVIEW_SHELL_META_TAG);
  metaElement.setAttribute("content", "true");
  document.head.appendChild(metaElement);
  return () => metaElement.remove();
}
