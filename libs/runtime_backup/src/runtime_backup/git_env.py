"""Shared environment helper for non-interactive git subprocess calls."""

import os


def git_noninteractive_env() -> dict[str, str]:
    """Environment for background git calls: never prompt for credentials.

    Git prompts for a username/password on the controlling TTY (bypassing
    captured stdout/stderr pipes) when a remote needs auth and no credential
    is available. For an unattended process that is fatal: the prompt blocks
    forever instead of failing. Two real cases motivated this helper:

    - bootstrap's "best-effort" runtime-worktree fetch against a PRIVATE
      origin (a mind created from a private inspiration repo) with no
      GH_TOKEN -- the prompt blocked bootstrap before supervisord ever
      started, so the workspace sat on "Loading workspace" indefinitely;
    - a runtime-backup push whose token has gone stale -- the prompt would
      wedge the backup loop forever.

    GIT_TERMINAL_PROMPT=0 turns the prompt into a fast failure, which every
    caller already handles (all of these git calls are best-effort by design:
    they log a nonzero exit and continue, or fall back to a local-only path).
    The returned dict is a fresh copy; ``os.environ`` is not mutated.
    """
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
