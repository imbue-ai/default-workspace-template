/**
 * The chat document: one page per chat (or per subagent view), served by the chat app at
 * `/<chat-id>` and framed by the workspace shell.
 */

import m from "mithril";
import "./style.css";
import { getChatAgentId, getChatId, getChatSessionId } from "./document-meta";
import { initChats } from "./models/Chats";
import { closeProviderChooser, isProviderChooserOpen, loadAccountsWithRetry } from "./models/Providers";
import { ProviderChooserModal } from "./views/ProviderChooserModal";
import { llmApi } from "./llm-api";
import type { LlmApi } from "./llm-api";
import { runHook } from "./hooks";
import { getFastModePromptChatId } from "./models/FastModePrompt";
import { trackBackendArrivals } from "./models/OutgoingMessages";
import { ChatPanel } from "./views/ChatPanel";
import { FastModeModal } from "./views/FastModeModal";
import { SubagentView } from "./views/SubagentView";
import { initShellPermissionResolutions } from "./views/permission-card";
import { connectChatToShell, isFrameRendered } from "./shell";

declare global {
  interface Window {
    $llm: LlmApi;
  }
  var $llm: LlmApi;
}

window.$llm = llmApi;

/** The page's one component: the chat (or the subagent view), plus the modals a chat can raise. */
function ChatDocument(chatId: string, agentId: string, sessionId: string): m.Component {
  return {
    view() {
      // The page is the whole frame: the shell sizes the frame to its pane, and everything
      // below (the transcript's scroll container, the composer) is laid out from this height.
      return m("div", { class: "chat-document flex flex-col", style: "height: 100vh" }, [
        sessionId === ""
          ? m(ChatPanel, { chatId, isVisible: isFrameRendered() })
          : m(SubagentView, { chatId, agentId, subagentSessionId: sessionId }),
        // The provider chooser: the page of a chat awaiting an account offers it, and the model
        // bar's "+ Add a provider" and a provider-fault notice open it from inside a chat.
        isProviderChooserOpen() ? m(ProviderChooserModal, { onDismiss: closeProviderChooser }) : null,
        getFastModePromptChatId() !== null ? m(FastModeModal) : null,
      ]);
    },
  };
}

async function bootstrap(): Promise<void> {
  const chatId = getChatId();
  const agentId = getChatAgentId();
  const sessionId = getChatSessionId();
  // The chat app's own WebSocket, read for this page's own chat.
  initChats();
  trackBackendArrivals();
  initShellPermissionResolutions();
  // Only the chat's own page reports the chat's presence: a subagent view is a second page
  // of the same chat in the same client, and its reports would overwrite the chat page's.
  connectChatToShell(chatId, { isPresenceReported: sessionId === "" });
  void loadAccountsWithRetry();
  const rootElement = document.getElementById("app");
  if (rootElement) {
    m.mount(rootElement, ChatDocument(chatId, agentId, sessionId));
    await runHook("ready");
  }
}

window.addEventListener("load", bootstrap);
