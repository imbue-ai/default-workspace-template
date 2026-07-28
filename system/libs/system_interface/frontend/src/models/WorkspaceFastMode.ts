/**
 * The workspace's fast-mode decision, and the state of the prompt that asks for it.
 *
 * New chat agents launch with fast mode on so the opening conversation feels
 * responsive. Fast mode costs more per token, so once a chat has run its grace
 * period (see the backend's `fast_mode_policy.py`, which owns the turn count) the
 * user is asked whether to keep it. The answer is recorded workspace-wide: every
 * chat agent created afterwards launches with it, and no chat asks again.
 *
 * The decision is workspace-global -- like Claude auth (see ClaudeAuth.ts) -- so a
 * single module-level record drives one shared modal rendered once in `App.ts`,
 * rather than every ChatPanel tracking its own.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { setFastMode } from "./ModelSettings";

export interface WorkspaceFastMode {
  is_decided: boolean;
  is_fast_mode_enabled: boolean;
  grace_turn_count: number;
}

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

export function openFastModePrompt(agentId: string): void {
  if (promptingAgentId === agentId) {
    return;
  }
  promptingAgentId = agentId;
  m.redraw();
}

/**
 * Record the user's answer and apply it to the agent that raised the prompt.
 *
 * Dismissing the modal routes here with `false`: the modal says so, and turning
 * fast mode off is the outcome that cannot surprise anyone with a bill. Other
 * chat agents already running keep their current setting until they restart;
 * only newly created ones read the recorded decision.
 */
export function resolveFastModePrompt(isFastModeEnabled: boolean): void {
  const agentId = promptingAgentId;
  promptingAgentId = null;
  // Reflect the answer immediately so the prompt cannot re-fire while the POST
  // is in flight; the response then replaces it with the server's own record.
  workspaceFastMode = {
    is_decided: true,
    is_fast_mode_enabled: isFastModeEnabled,
    grace_turn_count: workspaceFastMode?.grace_turn_count ?? 0,
  };
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
