/**
 * The chat document: one page per chat (or per subagent view), served by the chat app at
 * `/<agent-id>` and framed by the workspace shell (workspace app model, phase 6).
 */

import m from "mithril";
import "../style.css";
import { getChatAgentId, getChatSessionId } from "../base-path";
import { initAgentManager } from "../models/AgentManager";
import { closeProviderChooser, isProviderChooserOpen, loadAccountsWithRetry } from "../models/Providers";
import { ProviderChooserModal } from "../views/ProviderChooserModal";
import { llmApi } from "./llm-api";
import type { LlmApi } from "./llm-api";
import { runHook } from "./hooks";
import { getFastModePromptAgentId } from "./models/FastModePrompt";
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
function ChatDocument(agentId: string, sessionId: string): m.Component {
  return {
    view() {
      // The page is the whole frame: the shell sizes the frame to its pane, and everything
      // below (the transcript's scroll container, the composer) is laid out from this height.
      return m("div", { class: "chat-document flex flex-col", style: "height: 100vh" }, [
        sessionId === ""
          ? m(ChatPanel, { agentId, isVisible: isFrameRendered() })
          : m(SubagentView, { agentId, subagentSessionId: sessionId }),
        // The provider chooser: the model bar's "+ Add a provider" and a provider-fault
        // notice open it from inside a chat, so the page renders it as the shell does.
        isProviderChooserOpen() ? m(ProviderChooserModal, { onDismiss: closeProviderChooser }) : null,
        getFastModePromptAgentId() !== null ? m(FastModeModal) : null,
      ]);
    },
  };
}

async function bootstrap(): Promise<void> {
  const agentId = getChatAgentId();
  const sessionId = getChatSessionId();
  // The same agents WebSocket the shell uses (the process is shared until phase 10), read
  // for this page's own agent; the page is not a client of its own, so it registers none.
  initAgentManager({ isClientReported: false });
  trackBackendArrivals();
  initShellPermissionResolutions();
  // Only the chat's own page reports the chat's presence: a subagent view is a second page
  // of the same chat in the same client, and its reports would overwrite the chat page's.
  connectChatToShell(agentId, { isPresenceReported: sessionId === "" });
  void loadAccountsWithRetry();
  const rootElement = document.getElementById("app");
  if (rootElement) {
    m.mount(rootElement, ChatDocument(agentId, sessionId));
    await runHook("ready");
  }
}

window.addEventListener("load", bootstrap);
