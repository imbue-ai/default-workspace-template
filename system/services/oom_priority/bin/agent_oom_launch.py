#!/usr/bin/env python3
"""Agent launch wrapper: tag this process's memory-shedding band, then exec the harness.

Set as an agent type's ``command`` in ``.mngr/settings.toml`` so it becomes the
process mngr runs in the agent's tmux pane. The harness binary to run is this
script's FIRST argument (``claude``, ``codex``, ``pi``); everything after it is
passed through untouched. So the config reads::

    command = "python3 .../agent_oom_launch.py claude"

and mngr splices its own flags (``--settings``, ``--resume`` / ``--session-id``,
...) after that base, exactly as it would after a bare binary name.

It sets its *own* ``oom_score_adj`` to its priority band and records its pid in
the agent-pid registry, then ``exec``s the real harness.

Because it execs in place, the band-tagged process *is* the harness process (same
pid -- ``oom_score_adj`` and the pid both survive ``execve``), so every
subprocess the harness later spawns inherits the agent band by default; the
PreToolUse hook raises those subprocesses the rest of the way to the
most-expendable band. Because the process tags itself at launch, the band is set
before any subprocess exists -- the process that needs tagging is known directly,
with no process tree to inspect.

Harness-agnostic by construction: the band comes from the agent's label
(``MNGR_AGENT_NAME`` + the host records), never from which binary is being run.

ponytail: codex uses its ``command`` as the prefix for BOTH its visible ``--remote``
TUI and its ``app-server`` daemon, so a codex agent registers two pids under one
agent id. Both get the correct band at launch, which is the protection that matters,
and either being shed now produces a ledger record where before there was none.

Two known limits, both acceptable because they are strictly better than the
unbanded status quo:

- Shedding is asymmetric and only PROBABLY lands the right way. Killing the daemon
  takes the TUI with it (``codex --remote`` exits on lost connection) -- a clean
  full-agent death. Killing the TUI leaves the daemon orphaned in its detached
  sidecar window, still holding memory. Both carry the same band, so earlyoom picks
  by memory and the daemon -- the larger one -- goes first, which is the outcome we
  want. To make that deterministic rather than lucky, give the daemon a
  more-expendable band than the TUI (one more argument here).
- ``lookup_pid_by_agent_id`` returns the first live match, so the prioritizer's
  engagement re-tag reaches only one of the two. Fix by returning every live match
  and re-tagging each.

Separately, and outside this file: the shed NOTICE is claude-only
(``claude_shed_notice_hook.py`` is a claude SessionStart hook), so a shed codex
agent gets a ledger record but no in-session explanation on its next message.

The band comes from the agent's label, resolved from ``MNGR_AGENT_NAME`` + the
host records (see ``agent_identity``): a chat starts maximally expendable and is
protected later by live UI engagement; a worker or an unidentifiable agent starts
at the least-protected agent tier.

Tagging is best-effort: any failure (no writable ``/proc`` -- e.g. macOS -- or
host records that can't classify the agent) is swallowed so it can never block
the agent from starting. Exec is mandatory: if ``claude`` can't be launched the
failure propagates, since the agent cannot run without it.

Self-contained beyond the stdlib-only ``oom_priority`` package (imported via a
``sys.path`` insert), since this runs under a plain ``python3``.
"""

import os
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "src")
)

from oom_priority import bands
from oom_priority.agent_identity import is_chat_agent, is_primary_agent, is_worker_agent
from oom_priority.registry import record_agent_pid


def _band_for(agent_name: str) -> int:
    """Pick the launch band for ``agent_name``.

    - The primary (services) agent is pinned to the never-shed ``PRIMARY_AGENT``
      band (defensive; the primary never actually runs this wrapper).
    - A chat (``user_created``) starts at ``CHAT_AGENT_BASE``, the middle of the
      chat range. The system_interface prioritizer moves it either way from
      there -- down toward the protected floor as the user engages with it, up
      toward ``CHAT_AGENT_STALE_CEILING`` (past the worker band) as it is left
      alone -- so an un-re-tagged chat stays middling-expendable rather than
      pinned to the protected floor.
    - Everything else -- a worker, or an agent whose record we cannot read to
      classify -- lands at ``WORKER_AGENT``, the least-protected agent tier: an
      agent we cannot identify must not be shielded by our ignorance."""
    if is_primary_agent(agent_name):
        return bands.PRIMARY_AGENT
    if is_chat_agent(agent_name):
        return bands.CHAT_AGENT_BASE
    return bands.WORKER_AGENT


def _tag_self() -> None:
    """Set this process's band and register its pid (so a later kill of it maps
    back to this agent). No-op when ``MNGR_AGENT_NAME`` is unset."""
    agent_name = os.environ.get("MNGR_AGENT_NAME", "")
    if not agent_name:
        return
    is_worker = is_worker_agent(agent_name)
    band = _band_for(agent_name)
    pid = os.getpid()
    bands.set_oom_score_adj(pid, band)
    # Record the stable agent id too (when mngr exposes it) so the prioritizer can
    # resolve this pid by id to re-tag the chat at runtime.
    record_agent_pid(
        pid, agent_name, is_worker, agent_id=os.environ.get("MNGR_AGENT_ID") or None
    )


def main() -> None:
    # Tag before exec so the band (and registry entry) are in place the instant
    # the harness -- and any child it spawns -- exists. A tagging failure must never
    # stop the agent from launching: the band is an optimization, the harness is not.
    if len(sys.argv) < 2:
        # A misconfigured ``command`` (wrapper with no binary after it). Die loudly
        # rather than exec whatever flag mngr spliced first. SystemExit prints to
        # stderr itself, so this needs no print of its own.
        raise SystemExit("agent_oom_launch: missing harness binary argument")
    binary = sys.argv[1]
    try:
        _tag_self()
    except Exception as error:
        print(f"agent_oom_launch: tagging skipped: {error}", file=sys.stderr)
    os.execvp(binary, [binary, *sys.argv[2:]])


if __name__ == "__main__":
    main()
