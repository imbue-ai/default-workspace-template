/**
 * Global Claude auth-state for the in-UI login modal.
 *
 * Claude auth is mind-global: every agent reads the same host env file and
 * `CLAUDE_CONFIG_DIR`, so a broken auth state is never per-agent -- if one
 * agent is logged out, they all are. A single module-level `loginModalOpen`
 * flag therefore drives one shared `ClaudeLoginModal` (rendered once in
 * `App.ts`), rather than every `ChatPanel` subscribing and tracking its own
 * modal state.
 *
 * `openLoginModal` is called whenever any agent's transcript surfaces an
 * auth-error -- live over the SSE stream, or detected when a panel loads a
 * snapshot. `closeLoginModal` is the modal's dismiss handler. A fresh
 * auth-error after a dismiss reopens the modal.
 *
 * The modal only knows how to drive `claude auth login` (see
 * ClaudeLoginModal.ts), so callers must gate on harness first --
 * `shouldOpenLoginModalForHarness` is that gate, kept pure and exported here
 * (rather than inlined at each call site) so it's directly testable without
 * mounting the mithril component that calls it.
 */

import m from "mithril";

let loginModalOpen = false;

/** True for claude, or an unrecognized/missing harness (matches the backend's
 * own `parse_harness` fallback) -- false for a known non-claude harness,
 * where this modal would offer the wrong fix. */
export function shouldOpenLoginModalForHarness(harness: string | null | undefined): boolean {
  return harness == null || harness === "claude";
}

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
