/** Where a run's chat is: the chat app's root with the chat selected, on the chat app's own origin. */

import { deriveAppOrigin } from "@imbue/workspace-ui/src/origin";

import { getChatOriginLabel } from "../document-meta";

/** The chat's page on the chat app's origin: its minted label on this workspace's coordinate, or the bare
 *  name while the chat app has not registered (the shell matches the name only for an unlabelled app).
 *  ``host`` and ``protocol`` default to this page's own, as ``deriveAppOrigin``'s do. */
export function chatUrlOf(
  chatId: string,
  host: string = window.location.host,
  protocol: string = window.location.protocol,
): string {
  return `${deriveAppOrigin(getChatOriginLabel() || "chat", host, protocol)}?chat=${encodeURIComponent(chatId)}`;
}
