/**
 * The terminal-auth instructions notice -- the `terminal` agent-auth surface.
 *
 * Harnesses whose sign-in runs in their own TUI (codex, pi) declare
 * `auth_modal: "terminal"` plus an `auth_instructions` line on their catalog;
 * this shared notice renders that line for whichever agent raised it. The
 * managed counterpart is ClaudeLoginModal.
 */

import m from "mithril";
import { MODAL_MESSAGE_CLASS, Modal } from "./components/Modal";
import { getAgentById } from "../models/AgentManager";
import { getHarnessCatalog } from "../models/HarnessCatalog";
import { dismissAuthInstructions, getAuthInstructionsAgentId } from "../models/AgentAuth";
import { Button } from "./components/Button";

export function AgentAuthInstructionsModal(): m.Component {
  return {
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
          // On the Modal shell, so the listener exists only while the notice is
          // up (this component itself stays mounted, rendering null when closed).
          onEscape: dismissAuthInstructions,
          title: "Sign-in runs in the terminal",
          actions: [
            m(
              Button,
              {
                oncreate: (buttonVnode) => (buttonVnode.dom as HTMLButtonElement).focus(),
                onclick: () => dismissAuthInstructions(),
              },
              "OK",
            ),
          ],
        },
        [
          m("p", { class: MODAL_MESSAGE_CLASS }, [
            agent !== undefined ? m("strong", agent.name) : "This agent",
            " signs in through its own terminal. ",
            instructions,
          ]),
        ],
      );
    },
  };
}
