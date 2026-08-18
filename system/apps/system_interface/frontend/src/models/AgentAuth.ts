/**
 * The per-harness agent-auth surface dispatch.
 *
 * Every auth entry point (the composer's /login intercept, the stream
 * auth-error hook, the footer's "Agent auth" entry) routes through
 * `openAgentAuth`, which opens whatever surface the agent's harness declared
 * (`auth_modal` on the catalog): `managed` opens the in-app login modal
 * (claude's -- see ClaudeAuth.ts), `terminal` opens the shared instructions
 * notice showing the harness's `auth_instructions`. The frontend never
 * branches on the harness name.
 */

import m from "mithril";
import { getAgentById } from "./AgentManager";
import { getHarnessCatalog } from "./HarnessCatalog";
import { openLoginModal } from "./ClaudeAuth";

// The agent whose terminal-auth instructions notice is showing, or null.
let instructionsAgentId: string | null = null;

export function getAuthInstructionsAgentId(): string | null {
  return instructionsAgentId;
}

export function dismissAuthInstructions(): void {
  instructionsAgentId = null;
  m.redraw();
}

/** Open the agent-auth surface the agent's harness declared. An unknown harness
 *  or unloaded catalog falls back to the managed login modal -- the pre-harness
 *  behavior, and the only surface that exists without catalog data. */
export function openAgentAuth(agentId: string): void {
  const catalog = getHarnessCatalog(getAgentById(agentId)?.harness);
  if (catalog?.auth_modal === "terminal") {
    instructionsAgentId = agentId;
    m.redraw();
    return;
  }
  openLoginModal();
}
