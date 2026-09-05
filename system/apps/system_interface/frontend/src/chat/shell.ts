/**
 * The chat page's side of the workspace shell: the contract connection, and the two things a
 * chat page asks the shell for -- a sibling chat, and a subagent view -- both through
 * `shell:open`, since the page lives in its own document.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { postJson } from "../models/http";
import { CHAT_APP_NAME } from "../models/chatApp";
import { adoptClientIdentity } from "../models/ClientIdentity";
import { addressFor } from "../models/Inventory";
import { createChatAgent } from "./models/AgentManager";
import type { CreatedChatAgent } from "./models/AgentManager";
import { isEverythingView } from "../models/Projects";
import { connectToShell } from "../app_contract";
import type { ShellConnection, ShellHandshake } from "../app_contract";
import { currentPresenceState, reportPresence, startPresenceReporting } from "./presence";

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
 * Reading the layout keeps the panel's visibility in lockstep with the element, as the
 * shell's own surface state was before the split, so a redraw while hidden (a streamed
 * event) never runs the scroll management against a zero-height element.
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
   * Whether this page reports its presence for `agentId`. A chat's own page does; a subagent
   * view does not, because the chat app keeps one report per chat and client, and a second
   * page of the same chat in the same client would overwrite the chat page's own (as the
   * dock's chat panels alone counted before the split).
   */
  isPresenceReported: boolean;
}

/**
 * Connect the page for `agentId`: adopt the client identity the shell hands over, follow the
 * tab's visibility for the panel and (when this page reports it) for presence, and forward
 * focus so the shell activates the tab.
 */
export function connectChatToShell(agentId: string, options: ChatShellOptions): ShellConnection {
  const { isPresenceReported } = options;
  connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      adoptClientIdentity({ clientId: received.clientId, deviceKind: received.deviceKind, viewId: received.viewId });
      // Hidden until the shell says shown: a page can load into a background tab, and open
      // (any client's unexpired report) is what a hidden report keeps.
      if (isPresenceReported) startPresenceReporting(agentId, received.clientId, isShown ? "visible" : "hidden");
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
      startPresenceReporting(agentId, "direct-visit", document.visibilityState === "visible" ? "visible" : "hidden");
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
 * carries that project's id, as the launcher's tile does.
 */
export async function startChatOnAccount(accountId: string): Promise<void> {
  const viewId = shellViewId();
  const projectId = viewId !== "" && !isEverythingView(viewId) ? viewId : "";
  let created: CreatedChatAgent;
  try {
    created = await createChatAgent(projectId, accountId);
  } catch (e) {
    alert(`Failed to create chat: ${(e as Error).message}`);
    return;
  }
  connection?.open(chatAddress(created.agentId));
  m.redraw();
}

/**
 * Open the subagent view for `sessionId` of this page's chat beside it. The instance is
 * created first through the shell's relay (the chat app's `subagent` action), so the shell's
 * inventory lists it before the shell is asked to dock it.
 *
 * The relay route is reached on this page's own origin: the chat app shares the shell's
 * process, so the route is served here too.
 * CLEANUP: address the shell's own origin in phase 10 of the workspace app model, when the
 * chat app runs as its own process.
 */
export async function openSubagentTab(agentId: string, sessionId: string, description: string): Promise<void> {
  const key = `${agentId}.${sessionId}`;
  try {
    await postJson(apiUrl(`/api/apps/${CHAT_APP_NAME}/instances`), {
      action: "subagent",
      params: { parent: agentId, session: sessionId, description },
    });
  } catch (error) {
    console.warn(`[chat] could not create the subagent instance ${key}`, error);
  }
  connection?.open(chatAddress(key));
}
