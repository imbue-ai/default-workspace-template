/**
 * The terminal-auth instructions notice -- the `terminal` agent-auth surface.
 *
 * Harnesses whose sign-in runs in their own TUI (codex, pi) declare
 * `auth_modal: "terminal"` plus an `auth_instructions` line on their catalog;
 * this shared notice renders that line for whichever agent raised it. The
 * managed counterpart is ClaudeLoginModal.
 */

import m from "mithril";
import { Modal } from "./Modal";
import { getAgentById } from "../models/AgentManager";
import { getHarnessCatalog } from "../models/HarnessCatalog";
import { dismissAuthInstructions, getAuthInstructionsAgentId } from "../models/AgentAuth";

export function AgentAuthInstructionsModal(): m.Component {
  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      dismissAuthInstructions();
    }
  }

  return {
    oncreate() {
      document.addEventListener("keydown", handleKeydown);
    },

    onremove() {
      document.removeEventListener("keydown", handleKeydown);
    },

    view() {
      const agentId = getAuthInstructionsAgentId();
      if (agentId === null) {
        return null;
      }
      const agent = getAgentById(agentId);
      const instructions =
        getHarnessCatalog(agent?.harness)?.auth_instructions ?? "Sign in from the agent's terminal.";
      return m(
        Modal,
        {
          onDismiss: dismissAuthInstructions,
          title: "Sign-in runs in the terminal",
          actions: [
            m(
              "button.btn.btn--secondary",
              {
                oncreate: (buttonVnode: m.VnodeDOM) => (buttonVnode.dom as HTMLButtonElement).focus(),
                onclick: () => dismissAuthInstructions(),
              },
              "OK",
            ),
          ],
        },
        [
          m("p.modal-message", [
            agent !== undefined ? m("strong", agent.name) : "This agent",
            " signs in through its own terminal. ",
            instructions,
          ]),
        ],
      );
    },
  };
}
