/**
 * The chat page's side of the workspace shell: the contract connection, and the two things a
 * chat page asks the shell for -- a sibling chat, and a subagent view -- both through
 * `shell:open`, since the page lives in its own document.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { postJson } from "@imbue/workspace-ui/src/models/http";
import { adoptClientIdentity } from "@imbue/workspace-ui/src/models/ClientIdentity";
import { addressFor } from "@imbue/workspace-ui/src/addresses";
import { createChat, getChatById } from "./models/Chats";
import type { CreatedChat } from "./models/Chats";
import { isEverythingView } from "@imbue/workspace-ui/src/views";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellConnection, ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { currentPresenceState, reportPresence, startPresenceReporting } from "./presence";

/** The chat app's registered name: what its own pages address their instances under. */
const CHAT_APP_NAME = "chat";

export function chatAddress(instanceKey: string): string {
  return addressFor(CHAT_APP_NAME, instanceKey);
}

let connection: ShellConnection | null = null;
let handshake: ShellHandshake | null = null;
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

/** The view (project id, or Everything) the shell says this page's tab is in; "" until the handshake. */
export function shellViewId(): string {
  return handshake?.viewId ?? "";
}

export interface ChatShellOptions {
  /**
   * Whether this page reports its presence for `chatId`. A chat's own page does; a subagent
   * view does not, because the chat app keeps one report per chat and client, and a second
   * page of the same chat in the same client would overwrite the chat page's own.
   */
  isPresenceReported: boolean;
}

/**
 * Connect the page for `chatId`: adopt the client identity the shell hands over, follow the
 * tab's visibility for the panel and (when this page reports it) for presence, and forward
 * focus so the shell activates the tab.
 */
export function connectChatToShell(chatId: string, options: ChatShellOptions): ShellConnection {
  const { isPresenceReported } = options;
  connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      adoptClientIdentity({ clientId: received.clientId, deviceKind: received.deviceKind, viewId: received.viewId });
      // Hidden until the shell says shown: a page can load into a background tab, and open
      // (any client's unexpired report) is what a hidden report keeps.
      if (isPresenceReported) startPresenceReporting(chatId, received.clientId, isShown ? "visible" : "hidden");
      m.redraw();
    },
    onShown: () => {
      isShown = true;
      if (isPresenceReported) reportPresence("visible");
      m.redraw();
    },
    onHidden: () => {
      isShown = false;
      if (isPresenceReported) reportPresence("hidden");
      m.redraw();
    },
  });
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
  return connection;
}

/**
 * Open a new chat on `accountId` beside this one. The combo card's provider rows call this:
 * a chat binds to its account when it is created and nothing rebinds it, so "switch
 * provider" can only mean "start a chat on that one". A chat started inside a project
 * carries that project's id in its label; the shell files its tab when it docks the page.
 */
export async function startChatOnAccount(accountId: string): Promise<void> {
  const viewId = shellViewId();
  const projectId = viewId !== "" && !isEverythingView(viewId) ? viewId : "";
  let created: CreatedChat;
  try {
    created = await createChat(projectId, accountId);
  } catch (e) {
    alert(`Failed to create chat: ${(e as Error).message}`);
    return;
  }
  connection?.open(chatAddress(created.chatId));
  m.redraw();
}

/**
 * Open the subagent view for `sessionId` of this page's chat beside it. The instance is
 * created first through the chat app's own instances API (its `subagent` action, on this
 * page's origin), which nudges the shell, so the shell lists it before it is asked to dock it.
 * The session belongs to the chat's active agent, which is what the app keys the view on.
 */
export async function openSubagentTab(chatId: string, sessionId: string, description: string): Promise<void> {
  // CLEANUP: a chat the page does not list yet is its own first agent under the own-chat
  // rule; drop the fallback once the chat record store (phase 3 of the chat-agent split)
  // names the active agent for every chat.
  const agentId = getChatById(chatId)?.active_agent.agent_id ?? chatId;
  const key = `${chatId}.${agentId}.${sessionId}`;
  try {
    await postJson(apiUrl("/_instances"), {
      action: "subagent",
      params: { parent: chatId, session: sessionId, description },
    });
  } catch (error) {
    console.warn(`[chat] could not create the subagent instance ${key}`, error);
  }
  connection?.open(chatAddress(key));
}
