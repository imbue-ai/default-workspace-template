/**
 * The shell's cover over a first-run workspace: the provider page alone, with no chats behind it.
 */
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import { EMBEDDER_CAPABILITIES, setEmbedderMessageHandler } from "@imbue/workspace-ui/src/embed";

import { noteProviderRelayDeclined } from "../models/providerRelay";

/** Connect the cover's page to the shell. The page starts sign-ins itself, so it keeps what the chrome said of
 *  relaying them. The shell frames the cover as no window of its desktop and sends it no handshake: the page hears
 *  the chrome's own word, which the shell repeats to it once it has loaded. */
export function connectFirstRunCoverToShell(): ShellConnection {
  const cover = connectToShell({ capabilities: { navigation: false, closeChord: false } });
  setEmbedderMessageHandler(EMBEDDER_CAPABILITIES, (message) =>
    noteProviderRelayDeclined(message.declinesProviderRelay === true),
  );
  return cover;
}
