/**
 * The chat page's side of the workspace shell: the contract connection, and the two things a
 * chat page asks the shell for -- a sibling chat, and a subagent view -- both through the path
 * form of `shell:open` (desktop-interface contracts.md section 7), since the page lives in its
 * own document. A sibling chat is asked for at the chat root's path for it, so the shell opens a
 * root window showing it (the root that frames this page intercepts the request and selects the
 * chat in place instead, see root/relay.ts).
 *
 * The chat's shortcuts run here too, since a keystroke in this page never reaches the root that
 * frames it, and so does one the desktop app took from the OS for them (``shell:chord``): Stop the
 * agent presses this page's Stop button, and New chat starts one and asks for it as a sibling.
 */

import m from "mithril";
import { adoptClientIdentity } from "@imbue/workspace-ui/src/models/ClientIdentity";
import { addChatsUpdatedListener, createChat, getChatById } from "./models/Chats";
import {
  type ChatShortcut,
  listenForChatShortcutKeys,
  refreshKeybindings,
  runChatShortcutOfChord,
  setKeybindingSurface,
} from "./models/keybindings";
import { startNewChat } from "./root/newChat";
import type { CreatedChat } from "./models/Chats";
import type { ModelIdentity } from "./models/ModelSettings";
import { ensureParticipants } from "./models/Participants";
import { reportChatOnScreen } from "@imbue/workspace-ui/src/embed";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import type { ShellConnection, ShellHandshake } from "@imbue/workspace-ui/src/app_contract";
import { currentPresenceState, reportFocusChange, reportPresence, startPresenceReporting } from "./presence";
import type { ChatPageEmbedApi } from "./embedApi";
import { setCompactFromRoot } from "./compactLayout";
import { rootPathFor } from "./root/selection";
import {
  focusComposer,
  prependToComposer,
  setComposerHint,
  stopComposerAgent,
  stopDictationWhenHidden,
} from "./views/MessageInput";
import { mirrorParentTheme, wearThemeFromMessage } from "@imbue/workspace-ui/src/themes/themeClient";

/** The path of a sub-agent view: the chat, the agent whose session it is, and the session. */
export function subagentViewPath(key: string): string {
  return `/${key}`;
}

let connection: ShellConnection | null = null;
let lastHandshake: ShellHandshake | null = null;

/** The shell's last handshake to this page (or the root's, handed down), null before one. */
export function getShellHandshake(): ShellHandshake | null {
  return lastHandshake;
}
// Whether the shell says this page is on screen: true until told otherwise on a top-level
// visit, and false from the moment a framed page connects, until the shell says shown.
let isShown = true;

/**
 * Whether the frame's document is laid out at all: what the transcript's scroll management
 * keys on. A frame hidden with `display: none` (the chat root's held frames) drops the
 * document's layout in the same pass that the page's scroll container starts reporting zero
 * sizes (the frame's viewport, `innerHeight`, keeps its old value); the shell keeps a hidden
 * window's page laid out at its size, and its `shell:shown` and `shell:hidden` feed presence.
 * Reading the layout keeps the panel's visibility in lockstep with the element, so a redraw
 * while hidden (a streamed event) never runs the scroll management against a zero-height
 * element.
 */
export function isFrameRendered(): boolean {
  return document.documentElement.getBoundingClientRect().height > 0;
}

let isChatOnScreenReported = false;

/**
 * After a draw of this chat's transcript: tell the Imbue Studio chrome, once per page, that the chat is on screen.
 * The chrome keeps its wash over a new workspace until the welcome chat says this, so a transcript still loading or a
 * page the shell has not shown yet does not count.
 */
export function reportChatOnScreenOnce(chatId: string, isTranscriptLoaded: boolean): void {
  if (isChatOnScreenReported || !isTranscriptLoaded || !isShown || !isFrameRendered()) return;
  isChatOnScreenReported = true;
  reportChatOnScreen(chatId);
}

export interface ChatShellOptions {
  /**
   * Whether this page reports its presence for `chatId`. A chat's own page does; a subagent
   * view does not: it is not the chat, so its being on screen says nothing about whether the
   * user is reading the chat.
   */
  isPresenceReported: boolean;
  /** The path this page is served at, which it reports as its location (contracts.md section 7). */
  path: string;
}

/**
 * Connect the page for `chatId`: adopt the client identity the shell hands over, follow the
 * window's visibility for the panel and (when this page reports it) for presence, forward
 * focus so the shell raises the window, and (when this page reports presence) report the
 * document's focus as it comes and goes, framed or visited directly.
 */
export function connectChatToShell(chatId: string, options: ChatShellOptions): ShellConnection {
  const { isPresenceReported } = options;
  const onHandshake = (received: ShellHandshake): void => {
    lastHandshake = received;
    adoptClientIdentity({ clientId: received.clientId, desktopId: received.desktopId });
    // Hidden until the shell says shown: a page can load into a background tab, and open
    // (any page's unexpired visible or hidden report) is what a hidden report keeps.
    if (isPresenceReported) startPresenceReporting(chatId, received.clientId, isShown ? "visible" : "hidden");
    m.redraw();
  };
  const onShown = (): void => {
    isShown = true;
    if (isPresenceReported) reportPresence("visible");
    // Whoever joined while the page was out of view is drawn right once it is back.
    void ensureParticipants();
    m.redraw();
  };
  const onHidden = (): void => {
    isShown = false;
    if (isPresenceReported) reportPresence("hidden");
    // A page out of view stops dictating and keeps its words (the workspace-voice-input spec, D5).
    stopDictationWhenHidden();
    m.redraw();
  };
  // A chat page wears its desktop's theme: told by the shell when it frames the page itself, read off the chat
  // root when the root frames it (the shell never messages those).
  connection = connectToShell({
    onHandshake,
    onShown,
    onHidden,
    onTheme: (theme, revision, isPreview) => void wearThemeFromMessage({ theme, revision, isPreview }),
    onKeybindingsChanged: () => void refreshKeybindings(),
    onChordSurface: setKeybindingSurface,
    onChord: (chord) => runChatShortcutOfChord(chord, runChatShortcut),
  });
  void refreshKeybindings();
  listenForChatShortcutKeys(runChatShortcut);
  mirrorParentTheme();
  if (connection.isFramed) {
    // The chat root frames chat pages from this same origin and drives them by calling in
    // rather than by messaging (of the shell's messages, only the handshake comes down as one);
    // the shell's own frames ignore this, since a cross-origin parent cannot reach it.
    const embedApi: ChatPageEmbedApi = {
      shown: onShown,
      hidden: onHidden,
      prependDraft: (text) => prependToComposer(chatId, text),
      hintComposer: (hint) => setComposerHint(chatId, hint),
      setCompact: (isCompact) => {
        setCompactFromRoot(isCompact);
        m.redraw();
      },
      focus: () => {
        if (!focusComposer()) window.focus();
      },
      stopAgent: stopComposerAgent,
    };
    window.chatPageEmbed = embedApi;
  }
  if (!connection.isFramed) {
    // A direct visit has no shell to say when the page is showing; the document's own
    // visibility is the closest fact, and there is no shell-handed client id to report.
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
    window.addEventListener("focus", reportFocusChange);
    window.addEventListener("blur", reportFocusChange);
  }
  window.addEventListener("focus", () => {
    connection?.focused();
    void ensureParticipants();
  });
  reportChatLocation(chatId, options.path);
  return connection;
}

/** Run a chat shortcut pressed in this page: stop this chat's agent as its Stop button does, or start a new chat
 *  beside this one. */
function runChatShortcut(shortcut: ChatShortcut): void {
  if (shortcut === "stop-agent") {
    stopComposerAgent();
    return;
  }
  void startNewChat().then((outcome) => {
    if (outcome.kind === "refused") {
      alert(`Failed to create chat: ${outcome.detail}`);
      return;
    }
    connection?.openPath(rootPathFor(outcome.chatId), "focus");
  });
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
 * notice with neither. The chat is filed in no project: the desktop has none.
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
