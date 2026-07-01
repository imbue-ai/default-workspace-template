/**
 * Global GitHub auth-state for the in-UI login modal.
 *
 * GitHub auth is mind-global: `gh` writes a single credential store
 * (`~/.config/gh/hosts.yml`) that every agent shares, so a missing GitHub
 * login is never per-agent -- if `gh auth status` fails, it fails for all
 * agents. A single module-level `githubLoginModalOpen` flag therefore drives
 * one shared `GitHubLoginModal` (rendered once in `App.ts`), mirroring the
 * Claude login modal in `ClaudeAuth.ts`.
 *
 * `openGitHubLoginModal` is called from the AgentManager WebSocket dispatch
 * when the backend broadcasts `github_auth_required` -- the `/publish-inspiration`
 * skill posts to `/api/github-auth/require` when its own `gh auth status`
 * check fails. `closeGitHubLoginModal` is the modal's dismiss handler.
 */

import m from "mithril";

let githubLoginModalOpen = false;

export function isGitHubLoginModalOpen(): boolean {
  return githubLoginModalOpen;
}

export function openGitHubLoginModal(): void {
  if (githubLoginModalOpen) return;
  githubLoginModalOpen = true;
  m.redraw();
}

export function closeGitHubLoginModal(): void {
  if (!githubLoginModalOpen) return;
  githubLoginModalOpen = false;
  m.redraw();
}
