/**
 * The workspace's fast-mode decision, and the state of the prompt that asks for it.
 *
 * New chat agents launch with fast mode on so the opening conversation feels
 * responsive. Fast mode costs more per token, so once a chat has run its grace
 * period (see fast-mode-prompt.ts) the user is asked whether to keep it. The answer
 * is recorded workspace-wide: every chat agent created afterwards launches with it,
 * and no chat asks again.
 *
 * The decision is workspace-global -- like Claude auth (see ClaudeAuth.ts) -- so a
 * single module-level record drives one shared modal rendered once in `App.ts`,
 * rather than every ChatPanel tracking its own.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { setFastMode } from "./ModelSettings";

export interface WorkspaceFastMode {
  /** What new chats launch with, or null while the user has not answered. */
  fast_mode: boolean | null;
}

// Null until the first fetch lands, which is distinct from a loaded-but-unanswered
// decision ({fast_mode: null}) -- only the latter may raise the prompt.
let workspaceFastMode: WorkspaceFastMode | null = null;
let isFetchStarted = false;
// The agent whose conversation raised the prompt, or null when none is showing.
// Also the agent the answer is applied to live, since it is the one being used.
let promptingAgentId: string | null = null;

/** The workspace decision, or null until the first fetch lands. */
export function getWorkspaceFastMode(): WorkspaceFastMode | null {
  return workspaceFastMode;
}

export function getFastModePromptAgentId(): string | null {
  return promptingAgentId;
}

/** Load the decision once per page load. A failure leaves it null, which keeps
 *  the prompt from ever firing -- the safe direction, since a spurious prompt is
 *  worse than a missing one. */
export function fetchWorkspaceFastMode(): void {
  if (isFetchStarted) {
    return;
  }
  isFetchStarted = true;
  void m
    .request<WorkspaceFastMode>({ method: "GET", url: apiUrl("/api/workspace/fast-mode") })
    .then((value) => {
      workspaceFastMode = value;
      m.redraw();
    })
    .catch((error) => {
      console.warn("Failed to load the workspace fast-mode decision", error);
    });
}

/** Raise the shared prompt on behalf of `agentId`. The first conversation to
 *  claim it keeps it until it is answered: every mounted ChatPanel re-runs this
 *  check on every render, so letting a second agent take over would flip the
 *  owner (and schedule a redraw) on every frame while both are waiting. */
export function openFastModePrompt(agentId: string): void {
  if (promptingAgentId !== null) {
    return;
  }
  promptingAgentId = agentId;
  m.redraw();
}

/**
 * Record the user's answer and apply it to the agent that raised the prompt.
 *
 * Dismissing the modal routes here with `false`, since turning fast mode off is
 * the outcome that cannot surprise anyone with a bill. Other chat agents already
 * running keep their current setting; only newly created ones read the recorded
 * decision.
 */
export function resolveFastModePrompt(isFastModeEnabled: boolean): void {
  const agentId = promptingAgentId;
  promptingAgentId = null;
  // Reflect the answer immediately so the prompt cannot re-fire while the POST
  // is in flight; the response then replaces it with the server's own record.
  workspaceFastMode = { fast_mode: isFastModeEnabled };
  m.redraw();

  if (agentId !== null && !isFastModeEnabled) {
    // Only a switch to standard speed needs sending: the agent is already fast.
    setFastMode(agentId, false);
  }

  void m
    .request<WorkspaceFastMode>({
      method: "POST",
      url: apiUrl("/api/workspace/fast-mode"),
      body: { enabled: isFastModeEnabled },
    })
    .then((value) => {
      workspaceFastMode = value;
      m.redraw();
    })
    .catch((error) => {
      // The live agent still got the change; only the persisted default is lost,
      // so the prompt reappears in the next chat rather than silently sticking.
      console.warn("Failed to record the workspace fast-mode decision", error);
    });
}
