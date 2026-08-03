/**
 * Global open/close state for the workspace Settings overlay.
 *
 * Settings are mind-global (auth, connected services), not per-agent, so a
 * single module-level flag drives one shared `SettingsModal` rendered once in
 * `App.ts` -- the same pattern as the Claude login modal
 * (models/ClaudeAuth.ts). The header's gear button toggles it.
 */

import m from "mithril";

let settingsOpen = false;

export function isSettingsOpen(): boolean {
  return settingsOpen;
}

export function openSettings(): void {
  if (settingsOpen) return;
  settingsOpen = true;
  m.redraw();
}

export function closeSettings(): void {
  if (!settingsOpen) return;
  settingsOpen = false;
  m.redraw();
}
