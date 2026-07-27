/**
 * Global Claude auth-state for the in-UI login modal.
 *
 * Claude auth is mind-global: every agent reads the same shared
 * `CLAUDE_CONFIG_DIR` settings, so a broken auth state is never per-agent
 * -- if one agent is logged out, they all are. A single module-level
 * `loginModalOpen` flag therefore drives one shared `ClaudeLoginModal`
 * (rendered once in `App.ts`), rather than every `ChatPanel` subscribing
 * and tracking its own modal state.
 *
 * `openLoginModal` is called whenever any agent's transcript surfaces an
 * auth-error -- live over the SSE stream, or detected when a panel loads a
 * snapshot -- and from the persistent "Agent auth" entry in the chat
 * footer. `checkAuthStatusOnLoad` additionally runs once per page load so
 * a freshly created (never signed-in) mind opens the modal as its
 * onboarding step. `closeLoginModal` is the modal's dismiss handler. A
 * fresh auth-error after a dismiss reopens the modal.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

let loginModalOpen = false;
let loadCheckStarted = false;

export function isLoginModalOpen(): boolean {
  return loginModalOpen;
}

export function openLoginModal(): void {
  if (loginModalOpen) return;
  loginModalOpen = true;
  m.redraw();
}

export function closeLoginModal(): void {
  if (!loginModalOpen) return;
  loginModalOpen = false;
  m.redraw();
}

interface LoadCheckStatus {
  logged_in: boolean;
}

// One-shot page-load check: a mind with no credentials at all (the normal
// state right after creation, since the create flow no longer injects any)
// pops the modal without waiting for an agent to surface an auth error.
// Failures are ignored -- the transcript-driven detection still covers the
// broken-backend case once an agent actually errors.
export function checkAuthStatusOnLoad(): void {
  if (loadCheckStarted) return;
  loadCheckStarted = true;
  void m
    .request<LoadCheckStatus>({ method: "GET", url: apiUrl("/api/claude-auth/status") })
    .then((status) => {
      if (!status.logged_in) {
        openLoginModal();
      }
    })
    .catch(() => {
      // Status endpoint unavailable: stay quiet; reactive detection remains.
    });
}
