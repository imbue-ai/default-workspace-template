#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic helpers for pulling an old Minds workspace into this one.

The migration is mostly agent judgement -- deciding what a user-authored file
*means* in the new tree, whether a migrated app actually shows the user's own
data, which of two colliding ports to keep. This script owns the parts that are
mechanical, and therefore belong in tested code rather than agent prose. Every
subcommand prints JSON on stdout.

Source inspection (each reaches the old workspace over the brokered SSH session,
so all of these take the ``--ssh-*`` options):

``detect-layout``
    Classify the source tree as pre-declutter or current-layout by inspecting it,
    and report the roots (repo checkout, mngr host dir, worktrees) that follow
    from that. The version range is context, not the test: a pre-0.3.9 workspace
    has no version marker on disk at all.

``baseline-diff``
    Resolve the source's own template base -- the NEWEST first-parent
    template-state marker (``update-self:`` or ``Initial workspace commit``) --
    and diff the source's checked-out tree against it. That yields what the user
    authored in that workspace and excludes template-version drift by
    construction. No resolvable base means no automation.

``list-agents``
    Enumerate the source's agents from ``<host_dir>/agents/*/data.json`` and
    ``<host_dir>/preserved/``, and map each to its session JSONLs via
    ``claude_session_id_history``.

``classify-branches``
    Split the source's ``mngr/*`` branches into merged (tip already reachable
    from the checked-out branch) and unmerged (carrying work the migrated tree
    does not contain).

``list-ports`` / ``list-jobs``
    The source's registered app ports (registry file + supervisord program
    blocks) and its scheduled-job entries, with each job command path-rewritten
    for this workspace.

``audit-scan``
    Grep named source files for latchkey call sites, AI-integration call sites,
    hard-coded legacy paths, and references to retired skills.

Local (no SSH):

``map-paths``
    Map legacy repo-relative paths onto their current-layout counterparts,
    flagging the ones that are genuinely ambiguous rather than guessing.

``rewrite-refs``
    Rewrite legacy path references inside already-migrated files, reporting every
    substitution it made.

``recreate-agents``
    Recreate each enumerated agent here via ``mngr create --template chat
    --adopt <session.jsonl>`` and stop it, so its full history renders in a tab
    and a message revives it. Idempotent: already-recreated agents are skipped.

Deliberately NOT scripted: deciding an ambiguous path mapping, judging whether a
rewritten skill still means what it meant, semantic porting of template-file
edits, and the verification checklist. Those need open-ended reading, and a
helper that pretended to cover them would give the agent false confidence -- so
an incomplete or ambiguous result is reported as such and handed back to prose.

The git/ssh/mngr-touching subcommands are thin wrappers over the pure functions
below, which carry all the logic and are covered by ``migrate_workspace_test.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Mapping, NamedTuple, Sequence

# The old workspace could not be reached at all (host offline, or -- most often
# in a long pass -- an expired SSH grant). Distinct from a generic failure so the
# worker can re-broker the grant and retry rather than reporting stuck.
EXIT_SSH_UNREACHABLE = 3

# The source has no resolvable template base, so the baseline diff -- the one
# conclusive mechanism the automated flow rests on -- cannot be computed.
EXIT_NO_TEMPLATE_BASE = 4


# --- Source layout detection -----------------------------------------------

LAYOUT_PRE_DECLUTTER = "pre-declutter"
LAYOUT_CURRENT = "current"
LAYOUT_UNKNOWN = "unknown"


class SourceRoots(NamedTuple):
    """Where the source workspace keeps its checkout, mngr state, and worktrees.

    ``layout`` is one of :data:`LAYOUT_PRE_DECLUTTER`, :data:`LAYOUT_CURRENT`, or
    :data:`LAYOUT_UNKNOWN`; ``reason`` names the evidence, so an unknown verdict
    is explainable rather than just unhelpful.
    """

    layout: str
    repo_root: str
    host_dir: str
    worktrees_dir: str
    reason: str


_PRE_DECLUTTER_ROOTS = SourceRoots(
    layout=LAYOUT_PRE_DECLUTTER,
    repo_root="/mngr/code",
    host_dir="/mngr",
    worktrees_dir="/mngr/worktree",
    reason="",
)
_CURRENT_ROOTS = SourceRoots(
    layout=LAYOUT_CURRENT,
    repo_root="/home/user/workspace",
    host_dir="/home/user/.mngr",
    worktrees_dir="/home/user/worktrees",
    reason="",
)


def detect_layout(existing_paths: Sequence[str]) -> SourceRoots:
    """Classify the source layout from which of a probe set of paths exist.

    ``existing_paths`` is the subset of :data:`LAYOUT_PROBE_PATHS` that the
    source actually has. The test is the tree itself, never a version string: the
    reorganization shipped in no ``minds-v*`` tag, and a 0.3.8 workspace carries
    no version marker on disk (``VERSION_HISTORY.md`` only appeared between 0.3.8
    and 0.3.9). Ambiguous or unrecognized evidence returns
    :data:`LAYOUT_UNKNOWN` -- the flow stops and asks rather than migrating from
    a tree it cannot describe.
    """
    present = set(existing_paths)
    is_current = "/home/user/workspace/system" in present
    is_pre = "/mngr/code/runtime" in present or "/mngr/code/supervisord.conf" in present
    if is_current and not is_pre:
        return _CURRENT_ROOTS._replace(
            reason="/home/user/workspace/system exists: the current three-way system/ layout"
        )
    if is_pre and not is_current:
        return _PRE_DECLUTTER_ROOTS._replace(
            reason="the repo root is /mngr/code with a runtime/ directory and a root supervisord.conf"
        )
    if is_current and is_pre:
        return _CURRENT_ROOTS._replace(
            layout=LAYOUT_UNKNOWN,
            reason="both layouts' markers are present; the source tree is mid-migration or hand-edited",
        )
    return _CURRENT_ROOTS._replace(
        layout=LAYOUT_UNKNOWN,
        reason="neither layout's markers are present; this may not be a minds workspace checkout",
    )


# The paths whose existence decides the layout. Probed in one batched remote
# call (see ``_cmd_detect_layout``) rather than one round trip each.
LAYOUT_PROBE_PATHS: tuple[str, ...] = (
    "/home/user/workspace/system",
    "/home/user/workspace/data",
    "/mngr/code/runtime",
    "/mngr/code/supervisord.conf",
)


# --- Legacy path mapping ---------------------------------------------------


class PathMapping(NamedTuple):
    """One legacy path resolved onto the current layout.

    ``new_path`` is the mapping's best answer and ``rule`` the prefix that
    produced it. ``alternatives`` is non-empty exactly when the mapping is
    **ambiguous** -- the old tree collapsed a distinction the new one makes, so
    the answer depends on what the file *is*, which only reading it can settle.
    An ambiguous mapping is a question for the agent, not a result to apply.
    """

    old_path: str
    new_path: str
    rule: str
    alternatives: tuple[str, ...]

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.alternatives)


# Legacy repo-relative prefix -> current repo-relative prefix. Matched
# longest-prefix-first, so the per-package entries below win over the bare
# ``libs/`` and ``runtime/`` fallbacks. A trailing slash means "directory
# prefix"; an entry without one matches that exact path only.
LEGACY_PATH_MAP: Mapping[str, str] = {
    # The runtime/ + uploads/ state tree became the data/ scheme.
    "runtime/memory/": "data/memories/",
    "runtime/tickets/": "data/.tickets/",
    "runtime/harden/": "data/.tasks/",
    "runtime/secrets/": "data/.secrets/",
    "runtime/oom_priority/": "data/.state/oom_priority/",
    "runtime/applications.toml": "data/.state/apps.toml",
    "runtime/backup.toml": "data/system/backup.toml",
    "uploads/": "data/uploads/",
    "github_sync.toml": "data/system/github_sync.toml",
    # Root files that moved under system/ or docs/.
    "parent.toml": "system/config/parent.toml",
    "skills-lock.json": ".agents/skills-lock.json",
    "VERSION_HISTORY.md": "docs/VERSION_HISTORY.md",
    "supervisord.conf": "system/supervisord.conf",
    "Dockerfile": "system/Dockerfile",
    "style_guide.md": "docs/system/style_guide.md",
    "test_meta_ratchets.py": "system/test_meta_ratchets.py",
    "test_mngr_template_stacking.py": "system/test_mngr_template_stacking.py",
    "scripts/": "system/scripts/",
    "blueprint/": "docs/system/blueprint/",
    "specs/": "docs/system/specs/",
    "dev/changelog/": "system/changelog/",
    "changelog/": "system/changelog/",
    "vendor/": "system/vendor/",
    # The built-in packages, split three ways out of the old flat libs/.
    "apps/system_interface/": "system/apps/system_interface/",
    "libs/app_watcher/": "system/services/app_watcher/",
    "libs/bootstrap/": "system/libs/bootstrap/",
    "libs/browser/": "system/apps/browser/",
    "libs/cloudflare_tunnel/": "system/services/cloudflare_tunnel/",
    "libs/github_sync/": "system/libs/github_sync/",
    "libs/host_backup/": "system/services/host_backup/",
    "libs/mngr_cli_contract/": "system/libs/mngr_cli_contract/",
    "libs/oom_priority/": "system/services/oom_priority/",
    "libs/tk_command_parsing/": "system/libs/tk_command_parsing/",
}

# The two prefixes the old tree overloaded. ``libs/<pkg>`` held apps, background
# services, and support libraries alike; ``runtime/<name>`` held per-app data,
# per-skill state, machine state, and flow scratch. The new tree distinguishes
# all of those, so the mapping depends on what the thing *is* -- these produce a
# best guess plus the real alternatives, flagged ambiguous.
_AMBIGUOUS_PREFIXES: Mapping[str, tuple[str, ...]] = {
    "libs/": ("system/apps/", "system/services/", "system/libs/"),
    "runtime/": ("data/.apps/", "data/.skills/", "data/.state/"),
}


def _normalize_repo_relative(path: str) -> str:
    """Strip leading ``./`` segments and any leading ``/`` from a repo-relative path.

    Deliberately not ``lstrip("./")``, which strips *characters* and would turn
    ``.agents/skills/x`` into ``agents/skills/x``.
    """
    normalized = path
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def map_legacy_path(old_path: str) -> PathMapping:
    """Map one legacy repo-relative path onto the current layout.

    Longest matching prefix wins. A path already in the current layout (or with
    no legacy counterpart) maps to itself with an empty ``rule``. A path under an
    overloaded legacy prefix (see :data:`_AMBIGUOUS_PREFIXES`) comes back with
    ``alternatives`` populated: the first alternative is the best guess and the
    caller must decide, not apply.
    """
    normalized = _normalize_repo_relative(old_path)
    for prefix in sorted(LEGACY_PATH_MAP, key=len, reverse=True):
        if prefix.endswith("/"):
            if normalized.startswith(prefix):
                new = LEGACY_PATH_MAP[prefix] + normalized[len(prefix) :]
                return PathMapping(old_path, new, prefix, ())
        elif normalized == prefix:
            return PathMapping(old_path, LEGACY_PATH_MAP[prefix], prefix, ())
    for prefix, alternatives in _AMBIGUOUS_PREFIXES.items():
        if normalized.startswith(prefix):
            remainder = normalized[len(prefix) :]
            candidates = tuple(alt + remainder for alt in alternatives)
            return PathMapping(old_path, candidates[0], prefix, candidates)
    return PathMapping(old_path, normalized, "", ())


# Absolute roots that moved wholesale with the user-data layout, including the
# old image's ``/code`` and ``/worktree`` safety-net symlinks.
LEGACY_ABSOLUTE_MAP: tuple[tuple[str, str], ...] = (
    ("/mngr/code", "/home/user/workspace"),
    ("/mngr/worktree", "/home/user/worktrees"),
    ("/mngr", "/home/user/.mngr"),
    ("/code", "/home/user/workspace"),
    ("/worktree", "/home/user/worktrees"),
)

# The absolute repo roots a legacy reference can be qualified by, and the one it
# becomes. A repo-relative move has to be applied at both forms -- bare
# (``runtime/memory/x``) and root-qualified (``/mngr/code/runtime/memory/x``) --
# because the boundary rule that keeps ``runtime/`` from matching inside
# ``data/runtime/`` also keeps it from matching after a repo root.
_LEGACY_REPO_ROOTS: tuple[str, ...] = ("/mngr/code", "/code")
CURRENT_REPO_ROOT = "/home/user/workspace"


class Substitution(NamedTuple):
    """One rewrite the reference rewriter made, for its report."""

    line_number: int
    old_text: str
    new_text: str


def _boundaried(literal: str) -> re.Pattern[str]:
    """Match ``literal`` only where it starts a real path token.

    The lookbehind rejects an adjoining word character, ``/``, ``.``, or ``-``, so
    ``runtime/`` never matches inside ``myruntime/`` or ``data/runtime/`` (the
    latter already being a current-layout path). A literal that does *not* end in
    ``/`` additionally needs a trailing boundary, or ``/code`` would rewrite
    ``/coder``; a directory prefix needs none, since the ``/`` is the boundary and
    a lookahead would reject the very thing it prefixes.
    """
    trailing = "" if literal.endswith("/") else r"(?![\w-])"
    return re.compile(rf"(?<![\w./-]){re.escape(literal)}{trailing}")


def _rewrite_rules() -> list[tuple[str, str]]:
    """Build the ordered (legacy, current) literal pairs the rewriter applies.

    Three forms per repo-relative move -- root-qualified, bare-with-slash, and
    bare-without-slash -- plus the absolute-root moves, all sorted longest
    literal first so a specific rule always beats the generic one it sits inside
    (``/mngr/code/runtime/tickets/`` before ``/mngr/code`` before ``/mngr``).

    The no-slash form is generated only for a **multi-segment** legacy key. A
    single-segment one would produce a bare-word rule (``changelog``,
    ``scripts``, ``uploads``) that fires on ordinary English prose, whereas
    ``runtime/memory`` or ``dev/changelog`` is unambiguously a path.
    """
    rules: list[tuple[str, str]] = []
    for old, new in LEGACY_PATH_MAP.items():
        for root in _LEGACY_REPO_ROOTS:
            rules.append((f"{root}/{old}", f"{CURRENT_REPO_ROOT}/{new}"))
        rules.append((old, new))
        stem = old.rstrip("/")
        if old.endswith("/") and "/" in stem:
            for root in _LEGACY_REPO_ROOTS:
                rules.append(
                    (f"{root}/{stem}", f"{CURRENT_REPO_ROOT}/{new.rstrip('/')}")
                )
            rules.append((stem, new.rstrip("/")))
    rules.extend(LEGACY_ABSOLUTE_MAP)
    return sorted(rules, key=lambda rule: len(rule[0]), reverse=True)


def rewrite_legacy_references(text: str) -> tuple[str, list[Substitution]]:
    """Rewrite legacy absolute and repo-relative path references in ``text``.

    Applies the absolute-root moves and the unambiguous entries of
    :data:`LEGACY_PATH_MAP` (see :func:`_rewrite_rules` for the forms and their
    ordering). The ambiguous prefixes are deliberately left alone, since picking
    one silently is exactly the false confidence this script must not
    manufacture. Returns the rewritten text and one :class:`Substitution` per
    change, so every rewrite can be reported rather than applied invisibly.
    """
    rules = _rewrite_rules()
    substitutions: list[Substitution] = []
    rewritten_lines: list[str] = []
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        updated = line
        for old, new in rules:
            # A function replacement, not a template string, so nothing in the
            # destination path is read as a backreference.
            replaced = _boundaried(old).sub(lambda _match, new=new: new, updated)
            if replaced != updated:
                substitutions.append(Substitution(line_number, old, new))
                updated = replaced
        rewritten_lines.append(updated)
    return "".join(rewritten_lines), substitutions


# --- Template base resolution ----------------------------------------------

# The subjects that mark a commit as a *template state*: bootstrap writes
# ``Initial workspace commit`` on top of the cloned template, and update-self
# writes an ``update-self:`` merge. Both versions of the template write these, so
# the marker set is layout-independent.
_TEMPLATE_BASE_SUBJECT = "Initial workspace commit"
_TEMPLATE_BASE_PREFIX = "update-self:"


def find_template_base(first_parent_log: Sequence[str]) -> str | None:
    """Return the NEWEST template-state marker commit in a first-parent log.

    ``first_parent_log`` is ``git log --first-parent --format='%H %s'`` output,
    newest first. The newest marker is the template state the source last
    updated itself to, so diffing the working tree against it yields what the
    user authored *since* that state -- and excludes template-version drift by
    construction. (``update-self`` §5b walks the same markers but takes the
    OLDEST, because it wants where the mind started; the difference is
    load-bearing.) Returns ``None`` when no marker exists, which means the source
    cannot be migrated automatically.
    """
    for line in first_parent_log:
        stripped = line.strip()
        if not stripped:
            continue
        sha, _, subject = stripped.partition(" ")
        subject = subject.strip()
        if subject == _TEMPLATE_BASE_SUBJECT or subject.startswith(
            _TEMPLATE_BASE_PREFIX
        ):
            return sha
    return None


class BaselineEntry(NamedTuple):
    """One file the user authored or changed relative to the source's template base."""

    status: str
    path: str
    mapped_path: str
    is_ambiguous: bool
    alternatives: tuple[str, ...]


def parse_baseline_diff(
    name_status_lines: Sequence[str], layout: str
) -> list[BaselineEntry]:
    """Parse ``git diff --name-status`` output into mapped baseline entries.

    Each path is carried through :func:`map_legacy_path` when the source is
    pre-declutter, and left alone otherwise. Rename lines (``R100\\told\\tnew``)
    are reported at their new path -- that is where the content lives now.
    """
    entries: list[BaselineEntry] = []
    for line in name_status_lines:
        if not line.strip():
            continue
        fields = line.rstrip("\n").split("\t")
        status = fields[0]
        path = fields[-1]
        mapping = (
            map_legacy_path(path)
            if layout == LAYOUT_PRE_DECLUTTER
            else PathMapping(path, path, "", ())
        )
        entries.append(
            BaselineEntry(
                status=status,
                path=path,
                mapped_path=mapping.new_path,
                is_ambiguous=mapping.is_ambiguous,
                alternatives=mapping.alternatives,
            )
        )
    return entries


# --- Branch classification -------------------------------------------------


class BranchClassification(NamedTuple):
    """The source's ``mngr/*`` branches split by whether their work is already in the tree.

    ``merged`` branch tips are reachable from the source's checked-out branch, so
    the migrated tree already contains their work. ``unmerged`` ones carry work
    it does not -- each is a decision for the user, not something to drop.
    """

    merged: list[dict[str, str]]
    unmerged: list[dict[str, str]]


def classify_branches(
    ref_lines: Sequence[str], merged_branch_lines: Sequence[str], head_branch: str
) -> BranchClassification:
    """Split ``mngr/*`` branches into merged vs unmerged.

    ``ref_lines`` is ``git for-each-ref --format='%(objectname) %(refname:short)'
    refs/heads/mngr`` output; ``merged_branch_lines`` is ``git branch --merged``
    output (whose current-branch line is prefixed ``* ``). The checked-out branch
    itself is excluded from both buckets -- it is the migrated tree, not a
    branch with a disposition.
    """
    merged_names = {
        line.lstrip("*+ ").strip() for line in merged_branch_lines if line.strip()
    }
    classification = BranchClassification(merged=[], unmerged=[])
    for line in ref_lines:
        stripped = line.strip()
        if not stripped:
            continue
        sha, _, name = stripped.partition(" ")
        name = name.strip()
        if not name or name == head_branch:
            continue
        bucket = (
            classification.merged if name in merged_names else classification.unmerged
        )
        bucket.append({"branch": name, "tip": sha})
    return classification


# --- Agent enumeration and session resolution ------------------------------

# The one agent never brought over: it is the source workspace's primary agent
# (``is_primary=true``), and a second primary makes this workspace ambiguous to
# discovery. Its background services are provided here by our own primary.
EXCLUDED_AGENT_NAME = "system-services"

# The labels that survive recreation. The creation labels drive the OOM shedding
# bands, so a migrated chat must keep the one it had; ``project`` ties the agent
# to its workspace grouping.
_USER_CREATED_LABEL = "user_created"
_AGENT_CREATED_LABEL = "agent_created"
_CARRIED_LABELS = (_USER_CREATED_LABEL, _AGENT_CREATED_LABEL, "project")


class SourceAgent(NamedTuple):
    """One agent found on the source host, with the sessions it should resume."""

    name: str
    agent_id: str
    labels: dict[str, str]
    is_preserved: bool
    session_files: tuple[str, ...]
    unresolved_session_ids: tuple[str, ...]
    excluded_because: str


def is_excluded_agent(name: str, labels: Mapping[str, str]) -> str:
    """Return why an agent must not be recreated here, or ``""`` to bring it over.

    Only the source's primary agent is excluded, matched by its
    ``is_primary=true`` label and by name (a hand-made or partially-written
    ``data.json`` can be missing the label).
    """
    if labels.get("is_primary") == "true":
        return (
            "the source workspace's primary services agent; this workspace has its own"
        )
    if name == EXCLUDED_AGENT_NAME:
        return f"named {EXCLUDED_AGENT_NAME}: the source's primary services agent"
    return ""


def resolve_agent_sessions(
    history_text: str, session_paths: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve an agent's ``claude_session_id_history`` to session JSONL paths.

    ``history_text`` is that file's contents -- an append-only log of
    ``"<session_id> <source>"`` lines, oldest first. ``session_paths`` is the
    flat listing of ``<session_id>.jsonl`` files in the shared ``projects/``
    tree; this file is what makes the mapping possible at all, since every minds
    chat agent shares one ``CLAUDE_CONFIG_DIR`` and so all of their sessions sit
    in that one tree with nothing but the id to tell them apart.

    Returns ``(session_files, unresolved_ids)`` with the files in history order
    and duplicates dropped, so passing them to ``--adopt`` in order resumes the
    agent's most recent session. ``unresolved_ids`` are history entries with no
    file on disk (an aborted session, or one whose transcript was pruned); they
    are reported rather than silently dropped.
    """
    by_stem = {Path(path).stem: path for path in session_paths}
    session_files: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for line in history_text.splitlines():
        session_id = line.strip().split(" ")[0] if line.strip() else ""
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        resolved = by_stem.get(session_id)
        if resolved is None:
            unresolved.append(session_id)
        else:
            session_files.append(resolved)
    return tuple(session_files), tuple(unresolved)


def plan_agent_names(
    existing: Sequence[str], desired: Sequence[tuple[str, str]]
) -> dict[str, str]:
    """Assign each source agent a free name here, auto-suffixing collisions.

    ``desired`` is ``(agent_id, name)`` pairs and the result is keyed by
    **agent id**, not name: the inventory can legitimately hold two agents with
    the same name (a destroyed agent preserved under a name a later one reused),
    and a name-keyed result would collapse them into one.

    An agent-name collision is cosmetic -- the agent carries no wiring another
    agent could conflict with -- so it is resolved automatically as ``<name>-2``,
    ``<name>-3``, ... and reported. (Apps, skills, and ports collide for real and
    are never auto-resolved; the skill stops and asks for those.) Later entries
    also avoid names taken by earlier ones.
    """
    taken = set(existing)
    resolved: dict[str, str] = {}
    for agent_id, name in desired:
        candidate = name
        suffix = 2
        while candidate in taken:
            candidate = f"{name}-{suffix}"
            suffix += 1
        taken.add(candidate)
        resolved[agent_id] = candidate
    return resolved


def derive_recreate_labels(source_labels: Mapping[str, str]) -> list[str]:
    """Return the ``KEY=VALUE`` labels a recreated agent keeps.

    Carries the source's own creation label so the recreated agent lands in the
    same OOM shedding band, plus ``project``. When the source carried neither
    creation label (an early workspace, or a hand-made agent), defaults to
    ``user_created=true``: a migrated chat is the user's content, and an
    unbanded agent would otherwise be shed ahead of workers.
    """
    labels = [
        f"{key}={source_labels[key]}" for key in _CARRIED_LABELS if key in source_labels
    ]
    if not any(
        key in source_labels for key in (_USER_CREATED_LABEL, _AGENT_CREATED_LABEL)
    ):
        labels.insert(0, f"{_USER_CREATED_LABEL}=true")
    return labels


def build_recreate_argv(
    name: str,
    session_files: Sequence[str],
    source_labels: Mapping[str, str],
    mngr_binary: str = "mngr",
) -> list[str]:
    """Build the ``mngr create`` argv that recreates one agent here, dormant.

    ``--template chat`` matches how the system interface creates a chat agent, so
    the recreated agent gets a tab; ``--transfer none`` runs it in place in this
    workspace's checkout rather than cutting a worktree; each ``--adopt`` copies
    one session in, and the last one listed is the session it resumes.
    ``--no-connect`` keeps the create from attaching a terminal. A new agent id is
    minted deliberately: the old one may still be live on the old host.
    """
    argv = [
        mngr_binary,
        "create",
        name,
        "--template",
        "chat",
        "--transfer",
        "none",
        "--no-connect",
    ]
    for session_file in session_files:
        argv.extend(["--adopt", session_file])
    for label in derive_recreate_labels(source_labels):
        argv.extend(["--label", label])
    return argv


# --- App ports -------------------------------------------------------------


class AppPort(NamedTuple):
    """One app's registered name and port on the source, and where that was found."""

    name: str
    port: int
    url: str
    found_in: str


# The forward_port.py call every app's supervisord program block chains before
# its own start command. Reading the block rather than only the registry file
# matters: the registry is runtime state that a stopped workspace's app may never
# have written, while the block is committed.
_FORWARD_PORT_RE = re.compile(
    r"forward_port\.py\s+--url\s+(?P<url>http://localhost:(?P<port>\d+))\s+--name\s+(?P<name>[\w-]+)"
)


def parse_supervisord_ports(text: str) -> list[AppPort]:
    """Extract each app's name and port from ``forward_port.py`` calls in a supervisord config."""
    return [
        AppPort(
            name=match.group("name"),
            port=int(match.group("port")),
            url=match.group("url"),
            found_in="supervisord.conf",
        )
        for match in _FORWARD_PORT_RE.finditer(text)
    ]


# The registry's array-of-tables key: ``applications`` pre-rename, ``apps``
# after. Both hold ``name`` + ``url`` entries, so one parser covers a source of
# either vintage.
_REGISTRY_KEYS = ("apps", "applications")


def parse_apps_registry(toml_text: str) -> list[AppPort]:
    """Extract registered app names and ports from an apps/applications registry.

    Accepts both the current ``[[apps]]`` shape and the pre-rename
    ``[[applications]]`` one. An entry whose URL carries no parseable port is
    skipped -- it is a registration the migration cannot act on mechanically, and
    the supervisord scan is the authoritative source anyway.
    """
    parsed = tomllib.loads(toml_text)
    ports: list[AppPort] = []
    for key in _REGISTRY_KEYS:
        for entry in parsed.get(key, []):
            name = entry.get("name")
            url = entry.get("url", "")
            match = re.search(r":(\d+)", url)
            if not name or match is None:
                continue
            ports.append(
                AppPort(
                    name=name,
                    port=int(match.group(1)),
                    url=url,
                    found_in=f"registry [[{key}]]",
                )
            )
    return ports


def reconcile_ports(
    source_ports: Sequence[AppPort], local_ports: Sequence[AppPort]
) -> dict[str, list[dict[str, object]]]:
    """Split the source's app ports into free, name-colliding, and port-colliding.

    A collision on either the app *name* or the *port* is real wiring conflict --
    two apps cannot own one supervisord program name or one listening port -- so
    neither is auto-resolved here. They are returned for the skill to stop and
    ask about.
    """
    local_by_name = {port.name: port for port in local_ports}
    local_by_port = {port.port: port for port in local_ports}
    result: dict[str, list[dict[str, object]]] = {
        "free": [],
        "name_collisions": [],
        "port_collisions": [],
    }
    for port in source_ports:
        entry: dict[str, object] = {
            "name": port.name,
            "port": port.port,
            "found_in": port.found_in,
        }
        if port.name in local_by_name:
            entry["collides_with"] = local_by_name[port.name]._asdict()
            result["name_collisions"].append(entry)
        elif port.port in local_by_port:
            entry["collides_with"] = local_by_port[port.port]._asdict()
            result["port_collisions"].append(entry)
        else:
            result["free"].append(entry)
    return result


# --- Scheduled jobs --------------------------------------------------------


class CronEntry(NamedTuple):
    """One scheduled-job drop-in from the source, with its command path-rewritten."""

    job_name: str
    original: str
    rewritten: str
    is_commented: bool


def parse_cron_entries(job_name: str, text: str) -> list[CronEntry]:
    """Parse one ``/etc/cron.d``-style drop-in into its schedule lines, rewritten.

    Comment lines are carried through as ``is_commented`` entries rather than
    dropped: a commented-out line is a paused job the user may want back, and
    silently losing it would be indistinguishable from never having had it.
    Environment-assignment lines (``PATH=...``) are not schedule lines and are
    skipped.
    """
    entries: list[CronEntry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        is_commented = line.startswith("#")
        payload = line.lstrip("#").strip() if is_commented else line
        if not payload or re.match(r"^[A-Z_]+\s*=", payload):
            continue
        rewritten, _ = rewrite_legacy_references(payload)
        entries.append(
            CronEntry(
                job_name=job_name,
                original=payload,
                rewritten=rewritten,
                is_commented=is_commented,
            )
        )
    return entries


# --- Audit scanning --------------------------------------------------------

# What each audit kind looks for, and why the skill cares:
#
# ``latchkey``   -- every call site that will need a permission grant here.
#                   Grants are keyed to the *host*, so this workspace starts
#                   deny-all no matter what the old one had.
# ``ai``         -- every call into Claude, so the AI-integration review can
#                   move it onto the current credential resolver and decide
#                   whether re-snapshotting the credential is safe.
# ``legacy-path`` -- hard-coded old paths and retired substrate names, which
#                   resolve to nothing here.
# ``retired-skill`` -- references to skills that were renamed, which a migrated
#                   skill or doc will otherwise send the agent looking for.
AUDIT_PATTERNS: Mapping[str, tuple[re.Pattern[str], ...]] = {
    "latchkey": (
        # Any latchkey CLI invocation, allowing for interposed options such as
        # `latchkey --account alice@example.com curl ...`.
        re.compile(r"\blatchkey\b[^\n]*?\b(?:curl|services|auth)\b"),
        re.compile(r"latchkey-self\.invalid"),
        re.compile(r"permission-requests"),
        re.compile(r"LATCHKEY_GATEWAY"),
    ),
    "ai": (
        re.compile(r"claude_p\b"),
        re.compile(r"\bclaude\s+-p\b"),
        re.compile(r"\blitellm\b"),
        re.compile(r"ANTHROPIC_(?:API_KEY|BASE_URL)"),
        re.compile(r"read_workspace_ai_credentials"),
        re.compile(r"anthropic\.env"),
    ),
    "legacy-path": (
        re.compile(r"/mngr/(?:code|worktree)"),
        re.compile(r"(?<![\w./-])runtime/"),
        re.compile(r"(?<![\w./-])uploads/"),
        re.compile(r"applications\.toml"),
        re.compile(r"deferred-install"),
        re.compile(r"runtime-sync"),
    ),
    "retired-skill": (
        re.compile(r"build-web-service"),
        re.compile(r"update-service\b"),
        re.compile(r"(?:crystallize|update|heal|harden)-artifact"),
        re.compile(r"artifact-(?:skill|service|system-interface)"),
    ),
}


class AuditFinding(NamedTuple):
    """One audit hit: which file and line, which kind, and the matched text."""

    kind: str
    path: str
    line_number: int
    line: str
    matched: str


def scan_audit(
    files: Mapping[str, str], kinds: Sequence[str] | None = None
) -> list[AuditFinding]:
    """Scan file contents for every configured audit pattern.

    ``files`` maps path to contents; ``kinds`` restricts which
    :data:`AUDIT_PATTERNS` groups run (default: all). A line matching several
    patterns yields one finding per *kind*, not per pattern, so a call site with
    two latchkey markers is reported once.
    """
    selected = tuple(kinds) if kinds else tuple(AUDIT_PATTERNS)
    findings: list[AuditFinding] = []
    for path in sorted(files):
        for line_number, line in enumerate(files[path].splitlines(), 1):
            for kind in selected:
                for pattern in AUDIT_PATTERNS[kind]:
                    match = pattern.search(line)
                    if match is not None:
                        findings.append(
                            AuditFinding(
                                kind=kind,
                                path=path,
                                line_number=line_number,
                                line=line.strip()[:300],
                                matched=match.group(0),
                            )
                        )
                        break
    return findings


# --- SSH plumbing ----------------------------------------------------------


def build_ssh_argv(
    host: str, user: str, port: int, key_path: str, remote_command: str
) -> list[str]:
    """Build the argv that runs ``remote_command`` on the source workspace.

    ``BatchMode=yes`` keeps a failed or expired grant from hanging on a password
    prompt, and the two host-key options keep a re-brokered grant on a re-created
    container from tripping a known-hosts mismatch -- the grant itself is the
    authorization, and it is time-limited by the hub.
    """
    return [
        "ssh",
        "-i",
        key_path,
        "-p",
        str(port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"{user}@{host}",
        "--",
        remote_command,
    ]


class SshTarget(NamedTuple):
    """The brokered SSH connection to the source workspace."""

    host: str
    user: str
    port: int
    key_path: str


def _ssh_target(args: argparse.Namespace) -> SshTarget:
    return SshTarget(
        host=args.ssh_host,
        user=args.ssh_user,
        port=args.ssh_port,
        key_path=args.ssh_key,
    )


class SshError(Exception):
    """The source workspace could not be reached (offline host, or expired grant)."""


def run_remote(target: SshTarget, remote_command: str) -> str:
    """Run one shell command on the source and return its stdout.

    Raises :class:`SshError` when ssh itself fails to connect. A non-zero exit
    from the *remote command* is not an error here: several probes (a missing
    file, an empty glob) legitimately exit non-zero, and the callers below read
    the stdout they got.
    """
    result = subprocess.run(
        build_ssh_argv(
            target.host, target.user, target.port, target.key_path, remote_command
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    # OpenSSH reserves 255 for its own failures (connect refused, auth denied,
    # host unreachable); anything else is the remote command's own exit code.
    if result.returncode == 255:
        raise SshError(
            result.stderr.strip() or "ssh failed to connect to the source workspace"
        )
    return result.stdout


_FILE_SENTINEL = "===MIGRATE-WORKSPACE-FILE==="

# How many files one batched remote read asks for. Batching is what keeps a
# large audit set from costing one ssh handshake per file; the cap keeps the
# generated shell command well inside the remote shell's argument limit.
_READ_BATCH_SIZE = 60


def _read_remote_files(target: SshTarget, paths: Sequence[str]) -> dict[str, str]:
    """Read many remote files in as few round trips as possible, keyed by path.

    Remote reads go over the network, so batching matters: one handshake per file
    would dominate the whole pass. Each file is emitted between sentinel lines so
    the concatenated stream can be split back apart; a missing or unreadable file
    simply yields no section, and the caller reports it as unread rather than
    treating it as empty.
    """
    contents: dict[str, str] = {}
    unique = list(dict.fromkeys(paths))
    for start in range(0, len(unique), _READ_BATCH_SIZE):
        batch = unique[start : start + _READ_BATCH_SIZE]
        script = "; ".join(
            f"if [ -f {_shell_quote(path)} ]; then "
            f"echo {_shell_quote(_FILE_SENTINEL + ' ' + path)}; cat {_shell_quote(path)}; fi"
            for path in batch
        )
        contents.update(_split_file_stream(run_remote(target, script)))
    return contents


def _split_file_stream(stream: str) -> dict[str, str]:
    """Split a sentinel-delimited concatenation of remote file contents by path."""
    contents: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in stream.splitlines():
        if line.startswith(_FILE_SENTINEL + " "):
            if current is not None:
                contents[current] = "\n".join(buffer)
            current = line[len(_FILE_SENTINEL) + 1 :]
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        contents[current] = "\n".join(buffer)
    return contents


def _shell_quote(value: str) -> str:
    """Single-quote ``value`` for a POSIX shell."""
    return "'" + value.replace("'", "'\\''") + "'"


def _remote_git(target: SshTarget, repo_root: str, git_args: str) -> str:
    return run_remote(target, f"git -C {_shell_quote(repo_root)} {git_args}")


# --- Checkpointing ---------------------------------------------------------

# Where a pass keeps its cached scans and its agent-recreation ledger. Under
# ``data/.tasks/`` with the other flow-internal scratch.
DEFAULT_CHECKPOINT_DIR = "data/.tasks/migrate-workspace"


def _checkpoint_dir_value(args: argparse.Namespace) -> Path:
    """The ``--checkpoint-dir`` value, whether given before or after the subcommand.

    The attribute is absent (not defaulted) when the flag was never passed -- see
    the ``SUPPRESS`` note in :func:`main` -- so the default lives here.
    """
    return Path(getattr(args, "checkpoint_dir", DEFAULT_CHECKPOINT_DIR))


def _checkpoint_path(args: argparse.Namespace, name: str) -> Path:
    directory = _checkpoint_dir_value(args)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def _emit(args: argparse.Namespace, checkpoint_name: str, payload: object) -> int:
    """Print ``payload`` as JSON and checkpoint it under the flow's task dir.

    The checkpoint is what makes an interrupted pass resumable without
    re-scanning the source: a re-run with the same arguments serves the cached
    result unless ``--refresh`` is passed.
    """
    path = _checkpoint_path(args, checkpoint_name)
    text = json.dumps(payload, indent=2)
    path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def _cached(args: argparse.Namespace, checkpoint_name: str) -> bool:
    """Serve a previous run's checkpoint, if one exists and ``--refresh`` was not passed."""
    if getattr(args, "refresh", False):
        return False
    path = _checkpoint_dir_value(args) / checkpoint_name
    if not path.is_file():
        return False
    sys.stdout.write(path.read_text(encoding="utf-8"))
    return True


# --- Subcommands -----------------------------------------------------------


def _cmd_detect_layout(args: argparse.Namespace) -> int:
    if _cached(args, "layout.json"):
        return 0
    target = _ssh_target(args)
    probe = "; ".join(
        f"[ -e {_shell_quote(path)} ] && echo {_shell_quote(path)}"
        for path in LAYOUT_PROBE_PATHS
    )
    existing = [
        line.strip() for line in run_remote(target, probe).splitlines() if line.strip()
    ]
    roots = detect_layout(existing)
    return _emit(
        args,
        "layout.json",
        {
            "layout": roots.layout,
            "repo_root": roots.repo_root,
            "host_dir": roots.host_dir,
            "worktrees_dir": roots.worktrees_dir,
            "reason": roots.reason,
            "probed_present": existing,
        },
    )


def _cmd_baseline_diff(args: argparse.Namespace) -> int:
    if _cached(args, "baseline.json"):
        return 0
    target = _ssh_target(args)
    log_lines = _remote_git(
        target, args.repo_root, "log --first-parent --format='%H %s' HEAD"
    ).splitlines()
    base = find_template_base(log_lines)
    if base is None:
        print(
            json.dumps(
                {
                    "resolvable": False,
                    "reason": (
                        "no first-parent 'update-self:' or 'Initial workspace commit' marker: "
                        "this repo was not created by the workspace bootstrap, so there is no "
                        "template state to diff the user's own work against"
                    ),
                }
            )
        )
        return EXIT_NO_TEMPLATE_BASE
    name_status = _remote_git(
        target, args.repo_root, f"diff --name-status -M {base} -- ."
    ).splitlines()
    untracked = [
        line.strip()
        for line in _remote_git(
            target, args.repo_root, "ls-files --others --exclude-standard"
        ).splitlines()
        if line.strip()
    ]
    entries = parse_baseline_diff(name_status, args.layout)
    entries.extend(
        parse_baseline_diff([f"??\t{path}" for path in untracked], args.layout)
    )
    base_subject = _remote_git(
        target, args.repo_root, f"log -1 --format='%s' {base}"
    ).strip()
    return _emit(
        args,
        "baseline.json",
        {
            "resolvable": True,
            "base": base,
            "base_subject": base_subject,
            "layout": args.layout,
            "entries": [entry._asdict() for entry in entries],
            "ambiguous_count": sum(1 for entry in entries if entry.is_ambiguous),
            "caveat": (
                "Best-effort: the diff is exact about what changed since the template base, "
                "but a path mapping flagged ambiguous, and anything the user created outside "
                "the repo, still needs checking against the source tree."
            ),
        },
    )


def _cmd_list_agents(args: argparse.Namespace) -> int:
    if _cached(args, "agents.json"):
        return 0
    target = _ssh_target(args)
    host_dir = args.host_dir
    listing = run_remote(
        target,
        f"ls -1 {_shell_quote(host_dir + '/agents')} 2>/dev/null; "
        f"echo '---'; ls -1 {_shell_quote(host_dir + '/preserved')} 2>/dev/null; "
        f"echo '---'; find {_shell_quote(host_dir)} -path '*/projects/*' -name '*.jsonl' 2>/dev/null",
    )
    live_names, preserved_names, session_paths = _split_sections(listing, 3)
    read_targets: list[str] = []
    for entry in live_names:
        read_targets.append(f"{host_dir}/agents/{entry}/data.json")
        read_targets.append(f"{host_dir}/agents/{entry}/claude_session_id_history")
    for entry in preserved_names:
        read_targets.append(f"{host_dir}/preserved/{entry}/data.json")
        read_targets.append(f"{host_dir}/preserved/{entry}/claude_session_id_history")
    files = _read_remote_files(target, read_targets)

    agents: list[SourceAgent] = []
    unreadable: list[str] = []
    for parent, entries in (("agents", live_names), ("preserved", preserved_names)):
        for entry in entries:
            data_path = f"{host_dir}/{parent}/{entry}/data.json"
            raw = files.get(data_path)
            if raw is None:
                unreadable.append(data_path)
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                unreadable.append(data_path)
                continue
            labels = {str(k): str(v) for k, v in (data.get("labels") or {}).items()}
            name = str(data.get("name") or entry)
            history = files.get(
                f"{host_dir}/{parent}/{entry}/claude_session_id_history", ""
            )
            session_files, unresolved = resolve_agent_sessions(history, session_paths)
            agents.append(
                SourceAgent(
                    name=name,
                    agent_id=str(data.get("id") or entry),
                    labels=labels,
                    is_preserved=parent == "preserved",
                    session_files=session_files,
                    unresolved_session_ids=unresolved,
                    excluded_because=is_excluded_agent(name, labels),
                )
            )
    return _emit(
        args,
        "agents.json",
        {
            "host_dir": host_dir,
            "agents": [
                agent._asdict() for agent in sorted(agents, key=lambda a: a.name)
            ],
            "unreadable": unreadable,
            "caveat": (
                "An agent with no session file has no transcript to adopt -- it can be "
                "recreated empty or skipped, which is a question for the user."
            ),
        },
    )


def _split_sections(stream: str, count: int) -> list[list[str]]:
    """Split a ``---``-delimited batched-probe stream into ``count`` line lists."""
    sections: list[list[str]] = [[]]
    for line in stream.splitlines():
        if line.strip() == "---":
            sections.append([])
        elif line.strip():
            sections[-1].append(line.strip())
    while len(sections) < count:
        sections.append([])
    return sections[:count]


def _cmd_classify_branches(args: argparse.Namespace) -> int:
    if _cached(args, "branches.json"):
        return 0
    target = _ssh_target(args)
    head_branch = _remote_git(
        target, args.repo_root, "rev-parse --abbrev-ref HEAD"
    ).strip()
    refs = _remote_git(
        target,
        args.repo_root,
        "for-each-ref --format='%(objectname) %(refname:short)' refs/heads/mngr",
    ).splitlines()
    merged = _remote_git(
        target, args.repo_root, "branch --merged HEAD --list 'mngr/*'"
    ).splitlines()
    classification = classify_branches(refs, merged, head_branch)
    return _emit(
        args,
        "branches.json",
        {
            "head_branch": head_branch,
            "merged": classification.merged,
            "unmerged": classification.unmerged,
            "caveat": (
                "An unmerged branch carries work that is NOT in the migrated tree. Fetching it "
                "preserves the commits; what to do with each one is the user's decision."
            ),
        },
    )


def _cmd_list_ports(args: argparse.Namespace) -> int:
    if _cached(args, "ports.json"):
        return 0
    target = _ssh_target(args)
    repo_root = args.repo_root
    remote_paths = [
        f"{repo_root}/{rel}"
        for rel in (
            "system/supervisord.conf",
            "supervisord.conf",
            "data/.state/apps.toml",
            "runtime/applications.toml",
        )
    ]
    remote_files = _read_remote_files(target, remote_paths)
    source_ports: list[AppPort] = []
    for path, text in sorted(remote_files.items()):
        if path.endswith(".conf"):
            source_ports.extend(parse_supervisord_ports(text))
        else:
            source_ports.extend(parse_apps_registry(text))
    local_ports = _local_ports()
    return _emit(
        args,
        "ports.json",
        {
            "read": sorted(remote_files),
            "source_ports": [port._asdict() for port in source_ports],
            **reconcile_ports(source_ports, local_ports),
            "caveat": (
                "Name and port collisions are NOT auto-resolved: an app's program name and "
                "listening port are real wiring. Ask the user before renaming or re-porting one."
            ),
        },
    )


def _local_ports() -> list[AppPort]:
    """The app ports already taken in this workspace, from its own config and registry."""
    ports: list[AppPort] = []
    supervisord = Path("system/supervisord.conf")
    if supervisord.is_file():
        ports.extend(parse_supervisord_ports(supervisord.read_text(encoding="utf-8")))
    registry = Path("data/.state/apps.toml")
    if registry.is_file():
        ports.extend(parse_apps_registry(registry.read_text(encoding="utf-8")))
    return ports


def _cmd_list_jobs(args: argparse.Namespace) -> int:
    if _cached(args, "jobs.json"):
        return 0
    target = _ssh_target(args)
    # The durable copies under the repo are the definition; /etc/cron.d is the
    # live installation of them. Read both -- a job installed live but never
    # written durably would otherwise be invisible.
    listing = run_remote(
        target,
        f"ls -1 {_shell_quote(args.repo_root + '/data/.state/cron.d')} 2>/dev/null; "
        "echo '---'; ls -1 /etc/cron.d 2>/dev/null",
    )
    durable_names, live_names = _split_sections(listing, 2)
    durable_dir = f"{args.repo_root}/data/.state/cron.d"
    files = _read_remote_files(
        target,
        [f"{durable_dir}/{name}" for name in durable_names]
        + [f"/etc/cron.d/{name}" for name in live_names],
    )
    entries: list[CronEntry] = []
    for path, text in sorted(files.items()):
        entries.extend(parse_cron_entries(Path(path).name, text))
    return _emit(
        args,
        "jobs.json",
        {
            "durable": durable_names,
            "live": live_names,
            "entries": [entry._asdict() for entry in entries],
            "caveat": (
                "Each rewritten command must be verified to actually resolve in this workspace "
                "before the job is reported as scheduled -- a rewritten path can still name a "
                "script that was never migrated."
            ),
        },
    )


def _cmd_audit_scan(args: argparse.Namespace) -> int:
    if _cached(args, "audit.json"):
        return 0
    paths = _read_path_list(args.paths_from, args.paths)
    if args.ssh_host:
        files = _read_remote_files(_ssh_target(args), paths)
    else:
        files = {
            path: Path(path).read_text(encoding="utf-8", errors="replace")
            for path in paths
            if Path(path).is_file()
        }
    findings = scan_audit(files, args.kind or None)
    by_kind: dict[str, list[dict[str, object]]] = {kind: [] for kind in AUDIT_PATTERNS}
    for finding in findings:
        by_kind[finding.kind].append(finding._asdict())
    return _emit(
        args,
        "audit.json",
        {
            "scanned": sorted(files),
            "unreadable": sorted(set(paths) - set(files)),
            "findings": by_kind,
            "caveat": (
                "Pattern matching finds call sites, not intent. Every AI finding still needs a "
                "billing judgement, and a latchkey finding names a scope to request, not a "
                "grant to assume."
            ),
        },
    )


def _read_path_list(paths_from: str | None, paths: Sequence[str]) -> list[str]:
    """Collect the file list from ``--paths-from`` (one per line; ``-`` for stdin) and positionals."""
    collected = list(paths)
    if paths_from == "-":
        collected.extend(line.strip() for line in sys.stdin if line.strip())
    elif paths_from:
        collected.extend(
            line.strip()
            for line in Path(paths_from).read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return collected


def _cmd_map_paths(args: argparse.Namespace) -> int:
    mappings = [
        map_legacy_path(path) for path in _read_path_list(args.paths_from, args.paths)
    ]
    payload = {
        "mappings": [
            {
                "old_path": mapping.old_path,
                "new_path": mapping.new_path,
                "rule": mapping.rule,
                "is_ambiguous": mapping.is_ambiguous,
                "alternatives": list(mapping.alternatives),
            }
            for mapping in mappings
        ],
        "ambiguous_count": sum(1 for mapping in mappings if mapping.is_ambiguous),
    }
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_rewrite_refs(args: argparse.Namespace) -> int:
    report: list[dict[str, object]] = []
    for path in _read_path_list(args.paths_from, args.paths):
        file_path = Path(path)
        if not file_path.is_file():
            report.append({"path": path, "skipped": "not a regular file"})
            continue
        original = file_path.read_text(encoding="utf-8")
        rewritten, substitutions = rewrite_legacy_references(original)
        if substitutions and not args.dry_run:
            file_path.write_text(rewritten, encoding="utf-8")
        report.append(
            {
                "path": path,
                "changed": bool(substitutions) and not args.dry_run,
                "substitutions": [sub._asdict() for sub in substitutions],
            }
        )
    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "files": report,
                "caveat": (
                    "Only mechanical path references are rewritten; the ambiguous legacy "
                    "prefixes are left alone on purpose. Read each rewritten file end to end to "
                    "confirm it still means what it meant."
                ),
            },
            indent=2,
        )
    )
    return 0


def _cmd_recreate_agents(args: argparse.Namespace) -> int:
    inventory = json.loads(Path(args.agents_json).read_text(encoding="utf-8"))
    ledger_path = _checkpoint_path(args, "recreated-agents.jsonl")
    already: dict[str, str] = {}
    if ledger_path.is_file():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                already[record["source_agent_id"]] = record["created_name"]

    candidates = [
        agent for agent in inventory["agents"] if not agent["excluded_because"]
    ]
    pending = [agent for agent in candidates if agent["agent_id"] not in already]
    existing_names = _local_agent_names(args.mngr_binary)
    name_plan = plan_agent_names(
        [*existing_names, *already.values()],
        [(agent["agent_id"], agent["name"]) for agent in pending],
    )

    results: list[dict[str, object]] = []
    for agent in pending:
        created_name = name_plan[agent["agent_id"]]
        session_files = [
            str(Path(args.sessions_dir) / Path(remote).name)
            for remote in agent["session_files"]
        ]
        missing = [path for path in session_files if not Path(path).is_file()]
        if missing:
            results.append(
                {
                    "source_agent_id": agent["agent_id"],
                    "name": agent["name"],
                    "status": "skipped",
                    "reason": f"session files not staged locally: {missing}",
                }
            )
            continue
        argv = build_recreate_argv(
            created_name, session_files, agent["labels"], mngr_binary=args.mngr_binary
        )
        create = subprocess.run(argv, capture_output=True, text=True, check=False)
        if create.returncode != 0:
            results.append(
                {
                    "source_agent_id": agent["agent_id"],
                    "name": agent["name"],
                    "status": "failed",
                    "argv": argv,
                    "stderr": create.stderr.strip()[:2000],
                }
            )
            continue
        # Leave it dormant: the tab renders from the adopted JSONL regardless of
        # process state, and a message revives the agent. Recreating every old
        # agent *running* would flood the workspace with live processes.
        stop = subprocess.run(
            [args.mngr_binary, "stop", created_name],
            capture_output=True,
            text=True,
            check=False,
        )
        record = {
            "source_agent_id": agent["agent_id"],
            "source_name": agent["name"],
            "created_name": created_name,
            "renamed": created_name != agent["name"],
            "adopted_sessions": session_files,
            "stopped": stop.returncode == 0,
        }
        with ledger_path.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(record) + "\n")
        results.append({**record, "status": "created"})

    print(
        json.dumps(
            {
                "already_recreated": already,
                "excluded": [
                    {"name": agent["name"], "reason": agent["excluded_because"]}
                    for agent in inventory["agents"]
                    if agent["excluded_because"]
                ],
                "results": results,
                "ledger": str(ledger_path),
            },
            indent=2,
        )
    )
    return 0 if all(result["status"] != "failed" for result in results) else 1


def _local_agent_names(mngr_binary: str) -> list[str]:
    """The agent names already in use on this host."""
    result = subprocess.run(
        [mngr_binary, "ls", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [
        str(agent.get("name", ""))
        for agent in json.loads(result.stdout).get("agents", [])
    ]


# --- CLI -------------------------------------------------------------------


def _add_ssh_options(parser: argparse.ArgumentParser, required: bool) -> None:
    parser.add_argument(
        "--ssh-host", required=required, help="Source workspace SSH host."
    )
    parser.add_argument("--ssh-user", default="root", help="Source workspace SSH user.")
    parser.add_argument(
        "--ssh-port", type=int, default=22, help="Source workspace SSH port."
    )
    parser.add_argument(
        "--ssh-key",
        default="/tmp/mind_key",
        help="Private key of the brokered SSH grant.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI. Separate from :func:`main` so the wiring is exercisable."""
    # ``--checkpoint-dir`` and ``--refresh`` live on a shared parent parser so
    # they are accepted both before and after the subcommand (an option defined
    # only on the top-level parser would reject
    # ``migrate_workspace.py <subcommand> --refresh``). Both defaults must be
    # ``SUPPRESS``, not values: a subparser re-applies its own defaults over the
    # namespace the top-level parser already filled in, so a concrete default
    # here would SILENTLY discard a flag given *before* the subcommand -- writing
    # checkpoints to the wrong directory with no error. With ``SUPPRESS`` the
    # attribute is only set when the flag is actually passed, and
    # ``_checkpoint_dir_value`` / ``_cached`` supply the fallbacks. (Same reason
    # ``update_self.py`` suppresses its ``--repo-root``.)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--checkpoint-dir",
        default=argparse.SUPPRESS,
        help=f"Where progress and cached scans are checkpointed (default: {DEFAULT_CHECKPOINT_DIR}).",
    )
    common.add_argument(
        "--refresh",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Re-run instead of serving this pass's cached checkpoint.",
    )
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    layout_parser = sub.add_parser(
        "detect-layout",
        help="Classify the source tree as pre-declutter or current, and report its roots.",
        parents=[common],
    )
    _add_ssh_options(layout_parser, required=True)
    layout_parser.set_defaults(func=_cmd_detect_layout)

    baseline_parser = sub.add_parser(
        "baseline-diff",
        help="Diff the source's tree against its own template base to find the user's own work.",
        parents=[common],
    )
    _add_ssh_options(baseline_parser, required=True)
    baseline_parser.add_argument(
        "--repo-root",
        required=True,
        help="The source's repo checkout (from detect-layout).",
    )
    baseline_parser.add_argument(
        "--layout",
        required=True,
        choices=[LAYOUT_PRE_DECLUTTER, LAYOUT_CURRENT],
        help="The source's layout (from detect-layout); selects whether paths are mapped.",
    )
    baseline_parser.set_defaults(func=_cmd_baseline_diff)

    agents_parser = sub.add_parser(
        "list-agents",
        help="Enumerate the source's agents and the session files each should adopt.",
        parents=[common],
    )
    _add_ssh_options(agents_parser, required=True)
    agents_parser.add_argument(
        "--host-dir",
        required=True,
        help="The source's mngr host dir (from detect-layout).",
    )
    agents_parser.set_defaults(func=_cmd_list_agents)

    branches_parser = sub.add_parser(
        "classify-branches",
        help="Split the source's mngr/* branches into merged and unmerged.",
        parents=[common],
    )
    _add_ssh_options(branches_parser, required=True)
    branches_parser.add_argument(
        "--repo-root", required=True, help="The source's repo checkout."
    )
    branches_parser.set_defaults(func=_cmd_classify_branches)

    ports_parser = sub.add_parser(
        "list-ports",
        help="List the source's registered app ports and reconcile them against this workspace.",
        parents=[common],
    )
    _add_ssh_options(ports_parser, required=True)
    ports_parser.add_argument(
        "--repo-root", required=True, help="The source's repo checkout."
    )
    ports_parser.set_defaults(func=_cmd_list_ports)

    jobs_parser = sub.add_parser(
        "list-jobs",
        help="List the source's scheduled-job entries with their commands path-rewritten.",
        parents=[common],
    )
    _add_ssh_options(jobs_parser, required=True)
    jobs_parser.add_argument(
        "--repo-root", required=True, help="The source's repo checkout."
    )
    jobs_parser.set_defaults(func=_cmd_list_jobs)

    audit_parser = sub.add_parser(
        "audit-scan",
        help="Scan files for latchkey, AI-integration, legacy-path, and retired-skill references.",
        parents=[common],
    )
    _add_ssh_options(audit_parser, required=False)
    audit_parser.add_argument(
        "--kind",
        action="append",
        choices=sorted(AUDIT_PATTERNS),
        help="Restrict to one audit kind [repeatable]; default: all.",
    )
    audit_parser.add_argument(
        "--paths-from", help="File listing paths to scan, one per line ('-' for stdin)."
    )
    audit_parser.add_argument("paths", nargs="*", help="Paths to scan.")
    audit_parser.set_defaults(func=_cmd_audit_scan)

    map_parser = sub.add_parser(
        "map-paths",
        help="Map legacy repo-relative paths onto the current layout, flagging ambiguous ones.",
        parents=[common],
    )
    map_parser.add_argument(
        "--paths-from", help="File listing paths to map, one per line ('-' for stdin)."
    )
    map_parser.add_argument("paths", nargs="*", help="Legacy paths to map.")
    map_parser.set_defaults(func=_cmd_map_paths)

    rewrite_parser = sub.add_parser(
        "rewrite-refs",
        help="Rewrite legacy path references inside migrated files, reporting every substitution.",
        parents=[common],
    )
    rewrite_parser.add_argument(
        "--paths-from",
        help="File listing paths to rewrite, one per line ('-' for stdin).",
    )
    rewrite_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the substitutions without writing.",
    )
    rewrite_parser.add_argument("paths", nargs="*", help="Files to rewrite in place.")
    rewrite_parser.set_defaults(func=_cmd_rewrite_refs)

    recreate_parser = sub.add_parser(
        "recreate-agents",
        help="Recreate the source's agents here as dormant chats with their sessions adopted.",
        parents=[common],
    )
    recreate_parser.add_argument(
        "--agents-json", required=True, help="The list-agents output to recreate from."
    )
    recreate_parser.add_argument(
        "--sessions-dir",
        required=True,
        help="Local directory holding the session JSONLs copied off the source.",
    )
    recreate_parser.add_argument(
        "--mngr-binary", default="mngr", help="The mngr binary to call."
    )
    recreate_parser.set_defaults(func=_cmd_recreate_agents)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SshError as exc:
        print(
            json.dumps(
                {
                    "error": "ssh_unreachable",
                    "detail": str(exc),
                    "hint": (
                        "The source workspace could not be reached. The brokered SSH grant is "
                        "time-limited -- re-request it and retry; if that fails, the old host is "
                        "offline and must be started first."
                    ),
                }
            ),
            file=sys.stderr,
        )
        return EXIT_SSH_UNREACHABLE


if __name__ == "__main__":
    sys.exit(main())
