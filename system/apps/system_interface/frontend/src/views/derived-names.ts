/**
 * The display name each kind of object derives from its own identity.
 *
 * Objects are named the way minds names hosts: a human-readable display name
 * ("Chat 2", "Browser 1", "Terminal 3") paired with a canonical true name
 * ("Chat-2", "browser-1", "terminal-3") that is a deterministic transform of
 * it. The true name is the identity every ref, path and socket is keyed by;
 * these helpers derive the display half back from it, so nothing has to store
 * a display name separately for machine-minted objects.
 *
 * Objects created by older builds carry identities the transform does not
 * cover -- a chat agent with no `display_name` label, a browser with a random
 * english name -- and fall back to rendering that identity directly.
 */

/** What a chat agent's row and tab read: the `display_name` label mngr holds
 *  for it (what the user typed / the minted "Chat N"), else its true name. */
export function chatDisplayName(agent: { name: string; display_name?: string | null }): string {
  return agent.display_name != null && agent.display_name !== "" ? agent.display_name : agent.name;
}

const NUMBERED_TERMINAL_RE = /^terminal-([0-9]+)$/;
const NUMBERED_BROWSER_RE = /^browser-([0-9]+)$/;

/** "Terminal N" for an allocator-minted `terminal-<N>` tmux session; any other
 *  session (one the user made or renamed by hand) reads as itself. */
export function terminalDisplayName(sessionName: string): string {
  const match = NUMBERED_TERMINAL_RE.exec(sessionName);
  return match === null ? sessionName : `Terminal ${match[1]}`;
}

/** "Browser N" for a daemon-minted `browser-<N>` name; a browser created by an
 *  older build keeps its random name, prefixed so the row still says what it is. */
export function browserDisplayName(browserName: string): string {
  const match = NUMBERED_BROWSER_RE.exec(browserName);
  return match === null ? `Browser ${browserName}` : `Browser ${match[1]}`;
}

/** What a registered app is called before anyone renamed it. The one special
 *  case is the built-in file viewer, whose rail row has always read "File
 *  Viewer" (see Sidebar's SHORTCUT_ROWS) while its registered service name is
 *  `files` -- its instances ("File Viewer 2") and its menu verbs should say
 *  the same thing the row does. Every other app reads as its registered name. */
export function appServiceDisplayName(serviceName: string): string {
  return serviceName === "files" ? "File Viewer" : serviceName;
}
