/**
 * The chat page's side of the workspace shell: the contract connection, and the two things a
 * chat page asks the shell for -- a sibling chat, and a subagent view -- both through the path
 * form of `shell:open` (desktop-interface contracts.md section 7), since the page lives in its
 * own document. A sibling chat is asked for at the chat root's path for it, so the shell opens a
 * root window showing it (the root that frames this page intercepts the request and selects the
 * chat in place instead, see root/relay.ts).
 */

import m from "mithril";
import { adoptClientIdentity } from "@imbue/workspace-ui/src/models/ClientIdentity";
import { addChatsUpdatedListener, createChat, getChatById } from "./models/Chats";
import type { CreatedChat } from "./models/Chats";
import type { ModelIdentity } from "./models/ModelSettings";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellConnection, ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { currentPresenceState, reportPresence, startPresenceReporting } from "./presence";
import type { ChatPageEmbedApi } from "./embedApi";
import { rootPathFor } from "./root/selection";

/** The path of a sub-agent view: the chat, the agent whose session it is, and the session. */
export function subagentViewPath(key: string): string {
  return `/${key}`;
}

let connection: ShellConnection | null = null;
// Whether the shell says this page is on screen: true until told otherwise on a top-level
// visit, and false from the moment a framed page connects, until the shell says shown.
let isShown = true;

/**
 * Whether the frame's document is laid out at all: what the transcript's scroll management
 * keys on. A pane that stops showing this page hides the frame with `display: none`, which
 * drops the document's layout in the same pass that the page's scroll container starts
 * reporting zero sizes (the frame's viewport, `innerHeight`, keeps its old value); the
 * shell's `shell:shown` and `shell:hidden` follow a redraw later and feed presence instead.
 * Reading the layout keeps the panel's visibility in lockstep with the element, so a redraw
 * while hidden (a streamed event) never runs the scroll management against a zero-height
 * element.
 */
export function isFrameRendered(): boolean {
  return document.documentElement.getBoundingClientRect().height > 0;
}

export interface ChatShellOptions {
  /**
   * Whether this page reports its presence for `chatId`. A chat's own page does; a subagent
   * view does not, because the chat app keeps one report per chat and client, and a second
   * page of the same chat in the same client would overwrite the chat page's own.
   */
  isPresenceReported: boolean;
  /** The path this page is served at, which it reports as its location (contracts.md section 7). */
  path: string;
}

/**
 * Connect the page for `chatId`: adopt the client identity the shell hands over, follow the
 * window's visibility for the panel and (when this page reports it) for presence, and forward
 * focus so the shell raises the window.
 */
export function connectChatToShell(chatId: string, options: ChatShellOptions): ShellConnection {
  const { isPresenceReported } = options;
  const onHandshake = (received: ShellHandshake): void => {
    adoptClientIdentity({ clientId: received.clientId, desktopId: received.desktopId });
    // Hidden until the shell says shown: a page can load into a background tab, and open
    // (any client's unexpired report) is what a hidden report keeps.
    if (isPresenceReported) startPresenceReporting(chatId, received.clientId, isShown ? "visible" : "hidden");
    m.redraw();
  };
  const onShown = (): void => {
    isShown = true;
    if (isPresenceReported) reportPresence("visible");
    m.redraw();
  };
  const onHidden = (): void => {
    isShown = false;
    if (isPresenceReported) reportPresence("hidden");
    m.redraw();
  };
  connection = connectToShell({ onHandshake, onShown, onHidden });
  if (connection.isFramed) {
    // The chat root frames chat pages from this same origin and drives them by calling in
    // rather than by messaging (it never sends the shell's messages); the shell's own frames
    // ignore this, since a cross-origin parent cannot reach it.
    const embedApi: ChatPageEmbedApi = { handshake: onHandshake, shown: onShown, hidden: onHidden };
    window.chatPageEmbed = embedApi;
  }
  if (!connection.isFramed) {
    // A direct visit has no shell to say when the page is showing; the document's own
    // visibility is the closest fact, and there is no shell-handed client id to key on.
    if (isPresenceReported) {
      startPresenceReporting(chatId, "direct-visit", document.visibilityState === "visible" ? "visible" : "hidden");
      document.addEventListener("visibilitychange", () => {
        reportPresence(document.visibilityState === "visible" ? "visible" : "hidden");
      });
    }
  } else {
    isShown = false;
  }
  if (isPresenceReported) {
    window.addEventListener("pagehide", () => {
      if (currentPresenceState() !== "closed") reportPresence("closed");
    });
  }
  window.addEventListener("focus", () => connection?.focused());
  reportChatLocation(chatId, options.path);
  return connection;
}

/** Report where this page is and what it is called, now and whenever the chat's title changes.
 *
 * A chat's own page is titled after the chat; a sub-agent view reports no title, so the shell
 * titles its window after the app. */
function reportChatLocation(chatId: string, path: string): void {
  let reportedTitle: string | null = null;
  const report = (): void => {
    const title = getChatById(chatId)?.title ?? "";
    if (title === reportedTitle) return;
    reportedTitle = title;
    if (title !== "") document.title = title;
    connection?.location(path, title);
  };
  report();
  addChatsUpdatedListener(report);
}

/**
 * Open a new chat on `accountId` beside this one, with ``message`` as its first message when
 * given and ``pick`` as the model it runs on (null for the harness's default). The switch
 * dialog's "Start a new chat" calls this with the draft and the pick, and the failed-switch
 * notice with neither. The chat is filed in no project: the desktop has none, and the view id the
 * handshake still carries is the desktop's.
 */
export async function startChatOnAccount(
  accountId: string,
  message: string = "",
  pick: ModelIdentity | null = null,
): Promise<boolean> {
  let created: CreatedChat;
  try {
    created = await createChat("", accountId, message, pick);
  } catch (e) {
    alert(`Failed to create chat: ${(e as Error).message}`);
    return false;
  }
  connection?.openPath(rootPathFor(created.chatId), "focus");
  m.redraw();
  return true;
}

/**
 * Open the subagent view for `sessionId` of this page's chat beside it, by its path alone: the
 * view's key names the chat, the agent whose harness session it is (the chat's active agent,
 * else the chat's own id), and the session.
 */
export function openSubagentView(chatId: string, sessionId: string): void {
  const key = `${chatId}.${getChatById(chatId)?.active_agent.agent_id ?? chatId}.${sessionId}`;
  connection?.openPath(subagentViewPath(key), "focus");
}
