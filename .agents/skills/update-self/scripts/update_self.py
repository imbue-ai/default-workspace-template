#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Deterministic helpers for the safe, background-worker-driven update-self flow.

The update-self orchestration is mostly agent judgement (triage conflicts,
decide validation depth, work the report's impact analysis). This script owns
the parts that are *deterministic* and therefore belong in tested code rather
than agent prose:

``resolve-target``
    Resolve the ref to update to. Default is the latest **stable** ``minds-v*``
    tag (semver-sorted, ``-rc``/prerelease excluded) that is **not newer than the
    minds app driving this workspace**; an explicit override may name a specific
    tag, ``main``, or any other ref, and is reported back as exceeding the
    ceiling when it cannot be proven to sit at or below it.

    The ceiling exists because a workspace's template ships the code the outer
    app talks to (the system interface, the vendored ``mngr``), so updating past
    the app's own release would leave the workspace speaking a protocol its app
    does not know. It is read from the app itself (``GET /api/v1/app/version``,
    baseline-allowed through the latchkey gateway, no grant needed); when it
    cannot be read the command **fails** rather than silently updating uncapped.

    The output also carries ``held_back_by_ceiling`` -- whether the ceiling, and
    not the user, is why a newer release was not taken -- alongside
    ``latest_available``, the newest stable tag upstream *ignoring* the ceiling
    (``null`` if there is none) and so the release that flag names.

    A default target the workspace is **already on** is a refusal too: the command
    asks git whether the chosen ref is already an ancestor of ``HEAD``, rather
    than spending a backup, a worker, and a validation run on a merge that changes
    nothing. This is what makes the ceiling bite for a workspace sitting *at* it:
    with a newer release upstream the refusal names the app as the reason it
    cannot be had, and without one it is a plain "already up to date". A workspace
    *behind* the ceiling still updates to it.

``classify-merge``
    Split the files upstream changed into the reconciled **merged** set (local
    also diverged there -- validate) vs the clean **pulled-in** set (local left
    it untouched, so the merge just took upstream -- trust as upstream-tested),
    and map each file onto its change class and its test project. This drives
    both validation depth (merged set) and what ``apply`` must do to make the
    live workspace consistent with the merge. ``has_merge_work``
    is the mechanical half of the review-gate rule: true whenever the merged
    set is non-empty (any merge work at all happened). A false value is
    necessary but not sufficient to skip the gates -- the worker's impact
    analysis must also find no user-created code affected, and the worker must
    have authored no in-branch edits of its own (which this diff cannot see at
    all); the worker reference owns that half.

``changelog-entries``
    List ``changelog/`` entries newly added between two refs -- the raw input for
    the worker's "what's new" report.

``bootstrap-skill``
    Stage the copy of the update-self skill (SKILL.md, references, scripts) that
    the rest of the pass runs, at a single fixed path, and report whether it
    differs from the local copy. Normally that staged copy is the target ref's
    *own* copy (extracted from the already-fetched object); when the ref predates
    the skill it is the local copy instead. Either way the fixed path is left
    populated with a runnable flow, so the lead and worker can dispatch against it
    by literal path without carrying any value across shell invocations. This is
    what lets the flow, after resolving the target, hand off to the update-self
    process *as it exists at the version being updated to* -- so fixes to the
    update flow itself are applied live rather than being gated on the
    possibly-stale local copy. ``differs`` gates only which SKILL.md prose the
    lead follows, not the path.

``apply``
    Land a prepared merge and make the live workspace consistent with it, as
    one atomic, idempotent, rollback-on-failure motion inside a single
    near-OOM-exempt process: merge (fast-forward for update-self, ordinary for
    update-system-interface), pre-apply state snapshots, dependency refresh,
    provisioner run, frontend build (or the worker's already-built bundle),
    pre-flight, restart, health probes, the VERSION_HISTORY.md ledger entry,
    and ``env-converge upgrade``. On any failure it reverts the entire merge
    as a forward revert commit and restores the pre-apply snapshots -- plain
    file copies needing no network, no package manager, and no working
    ``mngr``. A full-information marker under ``data/.state/update-apply/``
    makes an interruption detectable: written before the merge, updated per
    phase, cleared on every exit path. Exit codes: 0 applied / 2 rolled back /
    3 emergency / 1 precondition (nothing changed).

``recover``
    Roll an interrupted apply back from its marker. ``--if-stale`` is the
    unattended guard (bootstrap at boot, the recovery cron every ~5 minutes):
    it acts only when the marker's recorded process is dead and the marker has
    gone a grace period without an update, and silently exits 0 in every
    normal state. ``--no-restart`` is the boot path (nothing is running yet,
    so disk state is the whole job). Bare ``recover`` is the explicit
    agent-driven rollback.

Impact analysis -- which services and skills depend on a changed file -- is
deliberately NOT scripted here: it requires open-ended exploration (imports,
shelled-out scripts, API-surface coupling) that a deterministic helper would
only pretend to cover. The worker reference owns that recipe.

The git-touching subcommands are thin wrappers over the pure functions below
(``pick_latest_stable_tag``, ``resolve_target``, ``classify_path``,
``classify_merge``), which carry all the logic and are covered by
``update_self_test.py``. ``fetch_app_template_ref`` is the one impure helper, kept
to the narrow job of turning a ``latchkey curl`` result into either a ref string or
a ``CeilingUnavailableError``.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple, Sequence

# The repo-relative directory holding the update-self skill (SKILL.md,
# references/, scripts/). Used by ``bootstrap-skill`` to extract the target
# ref's own copy of the flow.
SKILL_DIR_REL = ".agents/skills/update-self"

# --- Target resolution -----------------------------------------------------

# A released minds version tag, e.g. ``minds-v0.3.7`` (stable) or
# ``minds-v0.3.7-rc1`` (a release candidate -- a prerelease we never default to).
_TAG_RE = re.compile(r"^minds-v(\d+)\.(\d+)\.(\d+)(?:-(?P<pre>.+))?$")


class NoUpdateTargetError(ValueError):
    """Raised when no ref to update to could be chosen.

    A refusal, not a fault: the workspace is fine, there is simply nothing it may
    update to right now. Distinct from a plain ``ValueError`` so the CLI can render
    this case as a one-line explanation and let a genuine bug keep its traceback.
    """


class ResolvedTarget(NamedTuple):
    """The ref the update merges in, plus a coarse ``kind`` for the caller's log.

    ``kind`` is ``tag`` (a resolved ``minds-v*`` release), ``branch`` (``main``),
    or ``ref`` (any other override passed straight through for git to validate).

    ``ceiling`` is the template ref the app reported, passed through as given --
    it caps only insofar as it parses as a release, so a dev build's branch name
    is carried here and caps nothing. ``None`` means no ceiling was supplied at
    all, which only a direct caller does. ``exceeds_ceiling`` marks an override the
    ceiling could not vouch for: newer than the app, or a branch/commit carrying no
    version to compare; the default (no-override) path never sets it.
    """

    ref: str
    kind: str
    ceiling: str | None = None
    exceeds_ceiling: bool = False


class Version(NamedTuple):
    """A ``minds-v*`` tag's version, ordered by plain ``<`` the way semver orders.

    **Field order is the precedence order** -- comparison is tuple comparison, so
    reordering these silently changes which release outranks which.

    ``release_rank`` is 0 for a prerelease and 1 for the release it precedes, so
    ``0.4.0-rc1 < 0.4.0``; ``prerelease`` then breaks ties among prereleases of
    the same version. It has to be a *field* rather than a property derived from an
    empty ``prerelease``: only a field participates in the comparison.
    """

    major: int
    minor: int
    patch: int
    release_rank: int
    prerelease: tuple[tuple[int, int, str], ...]

    @property
    def is_stable(self) -> bool:
        """Whether this is a released version rather than a prerelease of one."""
        return not self.prerelease


def _prerelease_sort_key(pre: str) -> tuple[tuple[int, int, str], ...]:
    """Order a prerelease's dot-separated identifiers the way semver does.

    Numeric identifiers compare numerically and rank below alphanumeric ones, so
    ``rc.2`` follows ``rc.1`` rather than sorting lexically (where ``rc.10`` would
    land before ``rc.2``). Each identifier becomes ``(is_alphanumeric, number,
    text)`` so a single tuple comparison covers both kinds.

    "Numeric" is ``isdecimal``, semver's ``[0-9]+``, and not ``isdigit``, which
    also admits superscripts and other digits ``int()`` refuses to convert.
    """
    identifiers: list[tuple[int, int, str]] = []
    for identifier in pre.split("."):
        if identifier.isdecimal():
            identifiers.append((0, int(identifier), ""))
        else:
            identifiers.append((1, 0, identifier))
    return tuple(identifiers)


def parse_version(tag: str) -> Version | None:
    """Return the :class:`Version` of any ``minds-v*`` tag, prerelease included.

    Prereleases parse because a *ceiling* is a different question from a
    *candidate*: an app on ``minds-v0.4.0-rc1`` has a real version and should cap
    its workspaces. Candidate selection asks the separate question via
    :attr:`Version.is_stable`, so a prerelease still never wins the default
    "latest stable" pick.

    Ordering follows semver: a prerelease sorts below its own release, so a ceiling
    of ``minds-v0.4.0-rc1`` admits ``minds-v0.3.9`` but not ``minds-v0.4.0``.

    Returns ``None`` only for something that is not a release tag at all (a
    branch name, a bare commit) -- there is genuinely no version to compare.
    """
    match = _TAG_RE.match(tag.strip())
    if match is None:
        return None
    pre = match.group("pre")
    return Version(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        release_rank=0 if pre is not None else 1,
        prerelease=_prerelease_sort_key(pre) if pre is not None else (),
    )


def pick_latest_stable_tag(
    tags: Sequence[str], ceiling: str | None = None
) -> str | None:
    """Return the highest-versioned stable ``minds-v*`` tag, or ``None`` if none.

    Prereleases (``minds-v*-rc*``) and non-matching tags are ignored. Selection is
    by semantic version, not lexical order, so ``minds-v0.3.10`` beats
    ``minds-v0.3.9``.

    ``ceiling`` bounds the selection to tags at or below it, so a workspace never
    picks a template newer than the app driving it. It is parsed with
    :func:`parse_version`, so an app on a *prerelease* caps just as well as one on
    a stable release; only a ceiling that is not a release tag at all (a dev app
    reporting a branch) means no ceiling.

    Candidates are still filtered to *stable* tags: capping by a prerelease does
    not make one selectable.
    """
    ceiling_version = parse_version(ceiling) if ceiling is not None else None
    stable = [
        (version, tag)
        for tag in tags
        if (version := parse_version(tag)) is not None
        and version.is_stable
        and (ceiling_version is None or version <= ceiling_version)
    ]
    if not stable:
        return None
    return max(stable, key=lambda item: item[0])[1]


def is_held_back_by_ceiling(
    *,
    resolved_ref: str,
    latest_available: str | None,
    ceiling: str | None,
    has_override: bool,
) -> bool:
    """Whether the ceiling -- and not the user -- is why a newer release was not taken.

    Only true when the flow chose the target itself. With an explicit override the
    user picked the ref, so a gap between it and ``latest_available`` is their own
    doing; reporting "your app held this back" there blames the app for the user's
    choice (an ``--override`` to an *older* tag would otherwise trip it every time).
    """
    if has_override or ceiling is None or latest_available is None:
        return False
    return latest_available != resolved_ref


def _is_within_ceiling(ref: str, ceiling: str | None) -> bool:
    """Whether ``ref`` is provably a release at or below ``ceiling``.

    Both sides go through :func:`parse_version`, so a prerelease on either side
    compares properly rather than being written off. False for something with no
    version at all -- a branch or a bare commit -- where the ceiling genuinely
    cannot vouch for the ref. True when there is no ceiling to enforce.
    """
    ceiling_version = parse_version(ceiling) if ceiling is not None else None
    if ceiling_version is None:
        return True
    ref_version = parse_version(ref)
    return ref_version is not None and ref_version <= ceiling_version


def resolve_target(
    override: str | None,
    tags: Sequence[str],
    remote: str = "upstream",
    ceiling: str | None = None,
) -> ResolvedTarget:
    """Resolve the update target ref.

    With no override, pick the latest stable ``minds-v*`` tag at or below
    ``ceiling`` (raising if the upstream exposes none). An override of ``main``
    selects the template's default branch, **remote-qualified** to
    ``<remote>/main`` -- a bare ``main`` would resolve to the *local* branch, which
    ``git fetch upstream`` never advances, so the pull would merge stale local
    code. A tag, by contrast, lands in the local tag namespace on fetch and
    resolves by its bare name, so a known-tag override is returned as-is. Any
    other override is passed through verbatim as a ``ref`` for git to validate at
    fetch time (so a user can pin an arbitrary commit or a ref they've already
    qualified themselves).

    An override is never silently blocked -- the user asked for it by name -- but
    one that is not provably at or below ``ceiling`` comes back with
    ``exceeds_ceiling`` set, which the skill turns into an explicit user
    confirmation before anything is merged.
    """
    if override is None:
        latest = pick_latest_stable_tag(tags, ceiling=ceiling)
        if latest is None:
            raise NoUpdateTargetError(_no_target_message(tags, ceiling))
        return ResolvedTarget(latest, "tag", ceiling, False)
    exceeds = not _is_within_ceiling(override, ceiling)
    if override == "main":
        return ResolvedTarget(f"{remote}/{override}", "branch", ceiling, exceeds)
    if override in set(tags):
        return ResolvedTarget(override, "tag", ceiling, exceeds)
    return ResolvedTarget(override, "ref", ceiling, exceeds)


def _no_target_message(tags: Sequence[str], ceiling: str | None) -> str:
    """Explain why no default target could be picked, distinguishing the two causes."""
    if ceiling is not None and pick_latest_stable_tag(tags) is not None:
        return (
            f"every stable minds-v* tag upstream is newer than this workspace's minds "
            f"app ({ceiling}); update the app first, or pass an explicit --override "
            f"to update past it anyway"
        )
    return (
        "no stable minds-v* tag found upstream; pass an explicit "
        "--override (a tag, 'main', or a ref) to update anyway"
    )


def already_current_message(
    ref: str, latest_available: str | None, ceiling: str | None, is_held_back: bool
) -> str:
    """Explain that the default target is already merged, naming the ceiling when it is why.

    The two cases read very differently to a user and need different next steps.
    Held back: a newer release exists and the app is the only thing standing
    between them and it, so the message has to say so -- updating the app is the
    action that unblocks them. Not held back: the workspace is simply current,
    and there is nothing to do.
    """
    if is_held_back:
        return (
            f"this workspace is already on {ref}, the newest release your minds app "
            f"({ceiling}) supports; {latest_available} is available upstream but needs a "
            f"newer app -- update the app first, or pass an explicit --override to update "
            f"past it anyway"
        )
    return f"this workspace is already on {ref}, the newest release upstream; nothing to update"


# --- The app's update ceiling ----------------------------------------------

# The minds app's version route, addressed through the latchkey gateway's
# ``minds-api-proxy`` on the reserved gateway-self host. Allowed by the agent
# permissions baseline (``minds-app-version-read``), so this needs no grant and
# never raises a permission dialog -- which matters because update-self resolves
# its target from a background worker, with nobody watching to approve one.
_MINDS_APP_VERSION_URL = (
    "http://latchkey-self.invalid/minds-api-proxy/api/v1/app/version"
)

# Bounds the gateway round-trip, at the house network default (the style guide's
# 60s, matching this repo's other ``latchkey curl``, ``github_sync``'s
# ``_LATCHKEY_CURL_TIMEOUT_SECONDS``).
_APP_VERSION_TIMEOUT_SECONDS = 60

# Statuses that mean "this app predates the version route", not "something went
# wrong". 404 is the obvious one; 403 is in fact the *likelier* of the two, since
# the route and the gateway permission that reaches it (``minds-app-version-read``)
# ship in the same release, so an app old enough to lack the route also lacks the
# grant -- and the gateway denies an ungranted request before the app ever sees it.
_APP_TOO_OLD_STATUSES = frozenset({"403", "404"})


class CeilingUnavailableError(Exception):
    """Raised when the minds app's update ceiling could not be read.

    Never downgraded to "no ceiling": an app that cannot answer is very often an
    app too old to *have* this route, which is exactly the case the ceiling
    protects against.
    """


def fetch_app_template_ref(url: str = _MINDS_APP_VERSION_URL) -> str:
    """Return the newest workspace-template ref the running minds app supports.

    Goes through ``latchkey curl``, which injects the gateway credentials and
    passes every other argument (and curl's exit code) straight through. Each
    failure mode is reported distinctly, because the user's next action differs:
    a transport failure is worth retrying, an :data:`_APP_TOO_OLD_STATUSES`
    answer (403 or 404) means the app must be updated first, and any other bad
    status or malformed body is a bug worth reporting.
    """
    with tempfile.NamedTemporaryFile(suffix=".json") as body_file:
        try:
            result = subprocess.run(
                [
                    "latchkey",
                    "curl",
                    "--silent",
                    "--show-error",
                    "--output",
                    body_file.name,
                    "--write-out",
                    "%{http_code}",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=_APP_VERSION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CeilingUnavailableError(
                f"could not reach the minds app to read its version ({e}). The app may be "
                f"closed or the gateway down; retry once it is running."
            ) from e
        if result.returncode != 0:
            raise CeilingUnavailableError(
                f"could not reach the minds app to read its version (latchkey curl exited "
                f"{result.returncode}: {result.stderr.strip()}). The app may be closed or the "
                f"gateway down; retry once it is running."
            )
        status = result.stdout.strip()
        body = Path(body_file.name).read_text()

    if status in _APP_TOO_OLD_STATUSES:
        raise CeilingUnavailableError(
            "this workspace's minds app is too old to report its version (it answered "
            f"HTTP {status} for {url}), so there is no way to tell how far this workspace "
            "may safely update. Update the minds app itself first."
        )
    if status != "200":
        raise CeilingUnavailableError(
            f"the minds app returned HTTP {status} for its version ({body.strip()[:200]})."
        )
    try:
        template_ref = json.loads(body)["workspace_template_ref"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise CeilingUnavailableError(
            f"the minds app's version response could not be parsed ({e}): {body.strip()[:200]}"
        ) from e
    if not isinstance(template_ref, str) or not template_ref:
        raise CeilingUnavailableError(
            f"the minds app reported an empty workspace_template_ref: {body.strip()[:200]}"
        )
    return template_ref


# --- Change classification -------------------------------------------------

CLASS_SYSTEM_INTERFACE = "system_interface"
CLASS_SERVICE = "service"
CLASS_EDITABLE_TOOL = "editable_tool"
CLASS_SHARED_RUNTIME = "shared_runtime"
CLASS_PROVISIONER = "provisioner"
CLASS_DOCKERFILE = "dockerfile"
CLASS_DOCS = "docs"
CLASS_OTHER = "other"

# Files whose effects land at image-build / workspace-create / first-boot
# provisioning time -- the pinned global toolchain and the create/agent config --
# rather than at runtime. A change to one never reaches a *live* workspace by
# restarting a service (nothing running imports it): it needs the provisioning
# step re-run live (these scripts are idempotent) or a workspace rebuild. Split
# out of ``shared_runtime``/``other`` so the apply re-runs the provisioner for
# them instead of concluding there is nothing live to do.
_PROVISIONER_SCRIPTS = frozenset(
    {
        "system/scripts/setup_system.sh",  # pinned global toolchain (latchkey, uv, claude, ...)
        "system/scripts/install_secret_scanners.sh",  # pinned global scanner binaries
        "system/scripts/_provision_guard.sh",  # the guard that gates the above
    }
)


def _is_provisioner(path: str) -> bool:
    """Whether ``path`` shapes how the workspace/agent is *provisioned*.

    The pinned-toolchain scripts (:data:`_PROVISIONER_SCRIPTS`) plus everything
    under ``.mngr/`` -- the ``mngr create`` defaults, provider blocks, and the
    agent Claude-version pin that provisioning applies to every new workspace.
    """
    return path in _PROVISIONER_SCRIPTS or path.startswith(".mngr/")


# Basenames whose change means a dependency manifest moved, so the editable
# install / build needs its env refreshed rather than just picking up new source.
_MANIFEST_BASENAMES = frozenset(
    {"pyproject.toml", "uv.lock", "package.json", "package-lock.json"}
)


class PathClass(NamedTuple):
    """How one changed path should be applied and validated.

    ``reveal_class`` selects the go-live action -- named for the reveal step
    the atomic ``apply`` replaced, and kept because it is the wire name in
    ``classify-merge``'s JSON, which the skill and worker prose read;
    ``project`` is the pytest
    project whose suite covers the path (``.`` = the root workspace,
    ``system/apps/system_interface`` and ``system/vendor/mngr`` run their own suites);
    ``is_manifest`` flags a dependency-manifest change that needs an env refresh;
    ``requires_restart`` flags a path whose change must bounce the services agent
    before the workspace is consistent with the merged tree.
    """

    reveal_class: str
    project: str
    is_manifest: bool
    requires_restart: bool


def _project_for_path(path: str) -> str:
    """Return the pytest project root that owns ``path``.

    Only ``system/apps/system_interface`` and ``system/vendor/mngr`` carry their own pytest
    config (the root config ignores them); everything else -- libs, scripts,
    ``.agents`` -- is covered by the root suite, reported as ``.``.
    """
    if path.startswith("system/apps/system_interface/"):
        return "system/apps/system_interface"
    if path.startswith("system/vendor/mngr/"):
        return "system/vendor/mngr"
    return "."


def classify_path(path: str) -> PathClass:
    """Map a repo-relative path to its change class, test project, and manifest flag.

    The classes drive what ``apply`` does, and what the worker's report has to
    cover for the lead to finish the rest:

    - ``system_interface`` -- ``system/apps/system_interface/**``; the apply
      rebuilds or installs the bundle and refreshes the backend's environment
      on a manifest change (:func:`_refresh_backend_dependencies`).
    - ``service`` -- ``system/supervisord.conf`` and ``system/libs/bootstrap/**``; applied by
      restarting the services agent (``mngr start --restart system-services``,
      then ``system/scripts/refresh_workspace_view.py`` to rebuild the user's
      view, which the restart alone leaves showing the previous build).
    - ``editable_tool`` -- ``system/vendor/mngr/**``; ``.py`` picked up live, a manifest
      change needs ``uv sync --all-packages`` / an editable reinstall.
    - ``shared_runtime`` -- ``system/scripts/**``, other ``system/libs/**``,
      ``system/services/**``, ``system/apps/**``, and ``.agents/**``: may be a live runtime dependency of
      a service or a workspace-added skill or app, so it needs the worker's
      impact analysis before it can be called a silent merge.
    - ``provisioner`` -- the pinned-toolchain scripts and the ``.mngr/`` create
      config (see :func:`_is_provisioner`); shapes image-build / create-time
      provisioning, so a change is re-run live (idempotent scripts) or flagged
      for a workspace rebuild, never applied by a service restart alone.
    - ``dockerfile`` -- ``system/Dockerfile``; split by hunk into live-applicable
      vs rebuild-only by worker judgement.
    - ``docs`` -- a ``README.md`` or a ``changelog/*.md`` entry wherever it lives,
      ``CLAUDE.md``, and any other ``*.md`` outside the prefixes above. A
      ``SKILL.md`` under ``.agents/`` is *not* docs: a skill's prose is what an
      agent runs, so it stays ``shared_runtime``.
    - ``other`` -- anything else.

    ``requires_restart`` is orthogonal to the class: it names the paths whose
    change leaves a *live* process inconsistent with the merged tree until the
    services agent restarts. ``service`` always does. ``editable_tool`` does
    too: the vendored mngr is an editable install, so the moment the tree
    advances, the running system interface (which imports it in-process) is old
    code operating on new on-disk state -- "picked up live" only ever held for
    a fresh process. And ``.mngr/settings.toml`` alone among the provisioner
    paths: the running system interface re-reads it on every request, so a
    settings file newer than the code reading it must be paired with a restart
    or nothing live stops speaking the old schema. A ``.md`` file under either
    restart-requiring prefix keeps its prefix's class but never the restart:
    no live process holds documentation in memory (mngr's own help topics are
    read from disk per request through the editable install), and bouncing the
    services agent blips the user's UI.

    A supervisord-programmed ``system/services/**`` change is the deliberate
    exception: it leaves that program running pre-merge code, but the only
    restart this flag can ask for is the services agent's, which bounces every
    program at once. Activating one service precisely means restarting the
    individual programs a change touches, which cannot be inferred from paths
    alone -- so it stays with the worker's impact analysis and the lead (see
    the update-self skill's 5c), and this returns ``False``.
    """
    is_manifest = Path(path).name in _MANIFEST_BASENAMES
    project = _project_for_path(path)

    # A README or a per-PR changelog entry is documentation wherever it lives --
    # without this, one under a service prefix (e.g. ``system/libs/bootstrap/``)
    # would inherit that prefix's reveal class and trigger a pointless restart.
    # Every release ships entries under ``.agents/changelog/``, so this is the
    # common case rather than a corner. Matched one level deep and on ``.md``
    # only (the bucket glob ``**/changelog/*``), so an *app* named ``changelog``
    # keeps its own class.
    is_changelog_entry = Path(path).parent.name == "changelog" and path.endswith(".md")
    if Path(path).name == "README.md" or is_changelog_entry:
        return PathClass(CLASS_DOCS, project, is_manifest, False)
    # Provisioning files are matched before the generic ``system/scripts/`` and
    # catch-all rules below: a toolchain script lives under ``system/scripts/`` (would
    # otherwise read as ``shared_runtime``) and ``.mngr/settings.toml`` would
    # otherwise fall through to ``other`` -- either way the apply would miss
    # its build/create-time impact.
    if _is_provisioner(path):
        return PathClass(
            CLASS_PROVISIONER, project, is_manifest, path == ".mngr/settings.toml"
        )
    if path.startswith("system/apps/system_interface/"):
        return PathClass(CLASS_SYSTEM_INTERFACE, project, is_manifest, False)
    # Docs under a restart-requiring prefix (a non-README ``docs/*.md``, which
    # the README/changelog rule above does not catch) keep the class but not
    # the restart: nothing live holds ``.md`` content in memory.
    is_doc_file = path.endswith(".md")
    if path == "system/supervisord.conf" or path.startswith("system/libs/bootstrap/"):
        return PathClass(CLASS_SERVICE, project, is_manifest, not is_doc_file)
    if path.startswith("system/vendor/mngr/"):
        return PathClass(CLASS_EDITABLE_TOOL, project, is_manifest, not is_doc_file)
    if path == "system/Dockerfile":
        return PathClass(CLASS_DOCKERFILE, project, is_manifest, False)
    if (
        path.startswith("system/scripts/")
        or path.startswith(".agents/")
        or path.startswith("system/libs/")
        or path.startswith("system/services/")
        or path.startswith("system/apps/")
    ):
        return PathClass(CLASS_SHARED_RUNTIME, project, is_manifest, False)
    if path == "CLAUDE.md" or "/changelog/" in path or path.endswith(".md"):
        return PathClass(CLASS_DOCS, project, is_manifest, False)
    return PathClass(CLASS_OTHER, project, is_manifest, False)


class MergeClassification(NamedTuple):
    """The upstream-changed files split by disposition, with per-file class info.

    ``merged`` are files where local also diverged (reconcile + validate);
    ``pulled_in`` are clean upstream arrivals local left untouched (trust, but
    still apply). Each entry is a dict with ``path``, ``reveal_class``,
    ``project``, ``is_manifest``, ``requires_restart``, ``disposition``. The
    summary fields collect the
    distinct reveal classes and the projects whose suites the merged set implies.

    ``has_merge_work`` is true whenever the merged set is non-empty: any file
    that diverged on both sides means real merge work happened (a conflict, or
    git silently auto-merging both sides' edits), so the review gates must run.
    An empty merged set makes this false, which permits -- but does not by
    itself license -- skipping the gates: the worker must also establish that
    no user-created code depends on anything the update changed, and that it
    added no commits of its own on top of the merge. That last condition is
    invisible here by construction -- the caller passes the *pre-merge* local
    ref, so nothing the worker committed afterwards is in either diff.
    """

    merged: list[dict[str, object]]
    pulled_in: list[dict[str, object]]
    reveal_classes_merged: list[str]
    reveal_classes_pulled_in: list[str]
    projects_to_validate: list[str]
    has_merge_work: bool


def _entry(path: str, disposition: str) -> dict[str, object]:
    info = classify_path(path)
    return {
        "path": path,
        "reveal_class": info.reveal_class,
        "project": info.project,
        "is_manifest": info.is_manifest,
        "requires_restart": info.requires_restart,
        "disposition": disposition,
    }


def classify_merge(
    upstream_changed: Sequence[str], local_changed: Sequence[str]
) -> MergeClassification:
    """Split the upstream-changed files into the merged vs pulled-in sets.

    ``upstream_changed`` is the set of files upstream changed relative to the
    merge base; ``local_changed`` the set the local branch changed relative to
    the same base. A file in both diverged on both sides -> **merged** (validate);
    a file only upstream changed is a clean **pulled-in** arrival (trust). Files
    only *local* changed are not upstream updates at all and are ignored here.
    """
    local = set(local_changed)
    merged: list[dict[str, object]] = []
    pulled_in: list[dict[str, object]] = []
    for path in sorted(set(upstream_changed)):
        if path in local:
            merged.append(_entry(path, "merged"))
        else:
            pulled_in.append(_entry(path, "pulled_in"))

    def _distinct_classes(entries: list[dict[str, object]]) -> list[str]:
        return sorted({str(entry["reveal_class"]) for entry in entries})

    projects = sorted({str(entry["project"]) for entry in merged})
    return MergeClassification(
        merged=merged,
        pulled_in=pulled_in,
        reveal_classes_merged=_distinct_classes(merged),
        reveal_classes_pulled_in=_distinct_classes(pulled_in),
        projects_to_validate=projects,
        has_merge_work=bool(merged),
    )


# --- git-touching CLI wrappers ---------------------------------------------


def _git(args: Sequence[str], repo_root: Path) -> str:
    """Run a git command in ``repo_root`` and return its stdout (stripped)."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _list_names(output: str) -> list[str]:
    return [line for line in output.splitlines() if line]


def _is_already_merged(ref: str, repo_root: Path) -> bool:
    """Whether ``ref`` is already reachable from ``HEAD``, so merging it changes nothing.

    Cannot use :func:`_git` (``check=True``): exit 1 is the ordinary "not an
    ancestor" answer, not a failure. Any other code is a real git error -- a ref
    that does not resolve, or no ``HEAD`` at all -- and is raised rather than read
    as "not merged".
    """
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ref, "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        result.check_returncode()
    return result.returncode == 0


def _repo_root(args: argparse.Namespace) -> Path:
    """The ``--repo-root`` value, whether given before or after the subcommand.

    The attribute is absent (not defaulted) when the flag was never passed --
    see the ``SUPPRESS`` note in ``main`` -- so the cwd fallback lives here.
    """
    return getattr(args, "repo_root", Path.cwd())


def _cmd_resolve_target(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args)
    tags = _list_names(
        _git(["tag", "--list", "minds-v*"], repo_root)
        if args.local_tags
        else _git(["ls-remote", "--tags", "--refs", args.remote, "minds-v*"], repo_root)
    )
    if not args.local_tags:
        # ``ls-remote`` lines are ``<sha>\trefs/tags/<tag>``; take the tag.
        tags = [line.rsplit("/", 1)[-1] for line in tags]
    ceiling = args.ceiling if args.ceiling is not None else fetch_app_template_ref()
    target = resolve_target(args.override, tags, remote=args.remote, ceiling=ceiling)
    latest_available = pick_latest_stable_tag(tags)
    is_held_back = is_held_back_by_ceiling(
        resolved_ref=target.ref,
        latest_available=latest_available,
        ceiling=target.ceiling,
        has_override=args.override is not None,
    )
    # Only the default path: an override was asked for by name, and the rule that
    # it is never silently blocked outranks saving a no-op merge.
    if args.override is None and _is_already_merged(target.ref, repo_root):
        raise NoUpdateTargetError(
            already_current_message(
                target.ref, latest_available, target.ceiling, is_held_back
            )
        )
    print(
        json.dumps(
            {
                "ref": target.ref,
                "kind": target.kind,
                "ceiling": target.ceiling,
                "exceeds_ceiling": target.exceeds_ceiling,
                "latest_available": latest_available,
                "held_back_by_ceiling": is_held_back,
            }
        )
    )
    return 0


def _cmd_classify_merge(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args)
    # A --local that already contains --target is a degenerate invocation: the
    # merge base collapses to the target itself, the "upstream changed" diff is
    # empty, and an 800-file merge silently classifies as nothing at all. This
    # happens when the guide's post-merge `--local HEAD^1` is re-run after any
    # commit was added on top of the merge (HEAD^1 is then the merge commit,
    # not the pre-merge local). Refuse loudly instead of printing the empty
    # classification.
    contains = subprocess.run(
        ["git", "merge-base", "--is-ancestor", args.target, args.local],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if contains.returncode not in (0, 1):
        contains.check_returncode()
    if contains.returncode == 0:
        print(
            f"error: --local ({args.local}) already contains --target "
            f"({args.target}), so the merge base collapses to the target and "
            "every upstream change would classify as empty. Did you mean the "
            "merge commit's first parent? While HEAD is the merge commit that "
            "is --local HEAD^1; after further commits on top, name the merge "
            "commit itself (--local <merge-sha>^1).",
            file=sys.stderr,
        )
        return 1
    base = args.base or _git(["merge-base", args.local, args.target], repo_root)
    upstream_changed = _list_names(
        _git(["diff", "--name-only", base, args.target], repo_root)
    )
    local_changed = _list_names(
        _git(["diff", "--name-only", base, args.local], repo_root)
    )
    result = classify_merge(upstream_changed, local_changed)
    print(
        json.dumps(
            {
                "base": base,
                "merged": result.merged,
                "pulled_in": result.pulled_in,
                "reveal_classes_merged": result.reveal_classes_merged,
                "reveal_classes_pulled_in": result.reveal_classes_pulled_in,
                "projects_to_validate": result.projects_to_validate,
                "has_merge_work": result.has_merge_work,
            },
            indent=2,
        )
    )
    return 0


def _cmd_changelog_entries(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args)
    # Per-PR changelog entries live in a ``changelog/`` dir under each project
    # bucket -- ``system/changelog/``, ``.agents/changelog/``, and
    # ``system/{libs,services,apps}/<name>/changelog/`` (see
    # system/scripts/check_changelog_entries.py for the bucket definition).
    # Match every one of them at any depth with a single glob rather than one
    # dir alone, or the "what's new" digest silently drops everything landed
    # under the bucketed layout. Exclude the vendored subtree, which carries
    # its own separate changelog system.
    added = _list_names(
        _git(
            [
                "diff",
                "--name-only",
                "--diff-filter=A",
                args.base,
                args.target,
                "--",
                ":(glob)**/changelog/*",
                ":(exclude)system/vendor",
            ],
            repo_root,
        )
    )
    print(json.dumps({"added": added}))
    return 0


def _cmd_bootstrap_skill(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args).resolve()
    dest = Path(args.dest)
    dest_root = (dest if dest.is_absolute() else repo_root / dest).resolve()
    staged_skill = dest_root / SKILL_DIR_REL

    # Always stage into a clean dir. The flow runs the skill from ``staged_skill``
    # unconditionally (a single fixed path the lead and worker both reference by
    # literal -- no state carried across shell invocations), so this command must
    # leave a runnable copy there in *every* case, including the ref-predates-skill
    # fallback below.
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True)

    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{args.ref}:{SKILL_DIR_REL}"],
        cwd=repo_root,
        capture_output=True,
    )
    if exists.returncode != 0:
        # The target ref predates the skill, so there is no target copy to hand
        # off to: stage the *local* copy at the fixed path (so the worker still
        # finds the flow there) and report ``differs=False`` -- the caller stays
        # on the local flow. Skip ``__pycache__`` so stale bytecode caches
        # never ride along.
        shutil.copytree(
            repo_root / SKILL_DIR_REL,
            staged_skill,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        print(
            json.dumps(
                {"skill_dir": str(staged_skill), "differs": False, "ref": args.ref}
            )
        )
        return 0

    # Extract the ref's own copy of the skill via ``git archive`` (reads the
    # already-fetched object, no network, no working-tree mutation). The archive
    # lays the tree down under ``SKILL_DIR_REL``.
    archive = subprocess.run(
        ["git", "archive", args.ref, SKILL_DIR_REL],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest_root, filter="data")

    # Whether the ref's skill differs from the local working-tree copy. Let git
    # do the compare: ``git diff`` ignores untracked files, so the ``__pycache__/
    # *.pyc`` that importing the script drops into ``scripts/`` never registers as
    # a spurious difference. ``--quiet`` exits 0 if identical, 1 on any
    # difference; ``check_returncode`` surfaces any other code as a real git error.
    diff = subprocess.run(
        ["git", "diff", "--quiet", args.ref, "--", SKILL_DIR_REL],
        cwd=repo_root,
        capture_output=True,
    )
    if diff.returncode not in (0, 1):
        diff.check_returncode()
    differs = diff.returncode == 1
    print(
        json.dumps(
            {"skill_dir": str(staged_skill), "differs": differs, "ref": args.ref}
        )
    )
    return 0


# --- The atomic apply --------------------------------------------------------
#
# ``apply`` lands a prepared merge and makes the live workspace consistent with
# it, as one deterministic, idempotent, rollback-on-failure motion: merge,
# state snapshots, dependency refresh, provisioner run, frontend build (or the
# worker's already-built bundle), pre-flight, restart, health probes, the
# version-history ledger entry, and the environment converge. On any failure it
# reverts the entire merge and restores the pre-apply snapshots -- a recovery
# path needing no network, no package manager, and no working ``mngr``.
#
# It serves every update flow, not just update-self: ``update-system-interface``
# hands it an ordinary merge and its own already-built bundle, so both flows
# land the same way. What it must protect is therefore whole-repo -- the root
# venv, the two uv tool environments, ``node_modules`` and the built bundle are
# all copied aside first -- and what it must survive includes its own death,
# which is what the persistent marker and ``recover`` are for.

# The served app, the editable tool the live service runs from, and the build
# surfaces. These mirror system/scripts/build_workspace.sh -- the source of
# truth for how the served environment is constructed.
APP_DIR = "system/apps/system_interface"
FRONTEND_DIR = f"{APP_DIR}/frontend"
# The vendored mngr the workspace runs on, and the uv tool built from it. An
# editable install pins the *source path*, not the dependency closure -- so the
# moment a merge advances this tree, the ``mngr`` CLI starts running new code
# against whatever was resolved for the old code.
MNGR_VENDOR_DIR = "system/vendor/mngr"
MNGR_DIR = f"{MNGR_VENDOR_DIR}/libs/mngr"
MNGR_TOOL_NAME = "imbue-mngr"
MNGR_EXECUTABLE = "mngr"
TOOL_NAME = "system-interface"
# uv records how a tool was installed here, inside the tool's own directory.
_RECEIPT = "uv-receipt.toml"
# The frontend build output the backend serves at ``/``. Both ``node_modules``
# and this ``static/`` bundle are gitignored, so they never appear in a diff --
# they are protected by the pre-apply snapshots instead.
STATIC_DIR = f"{APP_DIR}/imbue/system_interface/static"
FRONTEND_BUILD_INDEX = f"{STATIC_DIR}/index.html"
# The identity stamp the frontend build writes into the bundle: the git tree
# hash of the frontend source directory the build ran from (an npm `postbuild`
# step in frontend/package.json; best-effort, absent when the build ran with no
# git repo). The apply compares it against the merged tree's own frontend tree
# hash, so a stale-but-populated bundle -- a wrong --worker-bundle path, or a
# build that silently emitted nothing over an old bundle -- fails or falls back
# instead of being served as if it were the merged source.
BUNDLE_STAMP_FILENAME = ".source-tree-hash"
# The pinned-toolchain provisioner, re-run live when a provisioner-classified
# path changed (idempotent; the content-addressed provision guard skips what
# already matches).
PROVISIONER_SCRIPT = "system/scripts/setup_system.sh"
# The environment the provisioner runs under when re-run live: the image
# build's, not the calling agent's. Root's passwd home moves to /home/user at
# runtime, so an agent-driven run carries HOME=/home/user, and an installer
# that follows $HOME then lands beside neither the checks nor the PATH entries
# the script fixes to /root/.local (this is what a Claude pin bump hit). Only
# the two values that diverge are pinned; everything else ambient (proxies,
# apt configuration) is kept.
_PROVISIONER_HOME = "/root"
_PROVISIONER_PATH = (
    "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)

DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"
ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
# The agent driving this apply -- recorded in the marker so recovery knows who
# to re-engage after an interruption.
ENV_DRI_AGENT = "MNGR_AGENT_NAME"

# Machine state for an in-flight apply. The marker is what makes a hard kill
# detectable (boot-time recovery, the recovery cron, a concurrent apply's
# refusal); the snapshots directory holds the pre-apply copies the rollback
# restores from. Both under ``data/.state`` so they survive a container
# restart, which ``/tmp`` need not.
STATE_DIR_REL = "data/.state/update-apply"
MARKER_FILENAME = "marker.json"
SNAPSHOTS_DIRNAME = "snapshots"
# The emergency record, written when a rollback could not put a healthy
# workspace back. The marker cannot carry this: it comes down on the emergency
# path (that exit is deliberate and fully reported, and re-running the same
# failed rollback from cron would not help), and the rollback has made the tree
# content match the pre-apply HEAD again -- so without a separate file the one
# state that most needs to speak is the one nothing can see.
EMERGENCY_FILENAME = "emergency.json"
# The provisioning-incomplete record, written when an update landed healthy
# but its provisioner run failed. A failed tool install leaves the tree and
# services consistent and re-running the provisioner is cheap and
# merge-independent, so it does not roll the whole release back -- but the
# gap must not be silent either: this record (the same durable shape as the
# emergency one) is what the skill reads to re-run the provisioner after the
# fix, and it comes down only when a provisioner run succeeds.
PROVISION_INCOMPLETE_FILENAME = "provision-incomplete.json"

# The apply's phases, recorded in the marker as each completes so an
# interrupted apply can be read (by recovery, and by the system interface's
# "an update was interrupted" banner) without guessing. The marker comes down
# at the apply's last rollback point -- once the live workspace is confirmed
# healthy on the merged tree -- so there is no phase past the restart: the
# post-success bookkeeping (ledger, env-converge) runs marker-free, because an
# interruption there must never read as an update worth rolling back.
PHASE_STARTED = "started"
PHASE_MERGED = "merged"
PHASE_SNAPSHOTTED = "snapshotted"
PHASE_REFRESHED = "environments_refreshed"
PHASE_PROVISIONED = "provisioned"
PHASE_BUILT = "frontend_built"
PHASE_RESTARTED = "restarted"

# The shared post-change refresh motion, repo-relative. It owns *how* a changed
# interface is pushed to whoever is looking; this script only decides *when*.
_REFRESH_SCRIPT = "system/scripts/refresh_workspace_view.py"
_REFRESH_TIMEOUT_SECONDS = 120.0

# Header the backend stamps on the app shell: ``false`` on the "not built"
# placeholder, ``true`` on the real app.
FRONTEND_BUILT_HEADER = "x-frontend-built"
# The hashed module script the built index.html loads -- what distinguishes the
# real app shell from the placeholder even on a backend too old for the header.
_ASSET_REFERENCE_PATTERN = re.compile(r"/assets/([A-Za-z0-9._-]+\.js)")

# Endpoints used to probe liveness. ``/api/agents`` exercises the mngr plugin
# discovery path -- exactly what a missing backend dependency or a broken
# plugin-config parse would take down.
HEALTH_PATH = "/api/agents"
SERVE_PATH = "/"

# Poll budgets. The health and pre-flight budgets are deliberately generous: a
# loaded workspace boots a healthy backend well past the 30s the old reveal
# allowed, and a budget that is too short reads as "your change was bad" over a
# change that was fine -- with the whole release as blast radius and a retry
# that is correctly refused. A budget that is too long costs seconds only on a
# genuinely broken change (the pre-flight also stops early when the boot
# process dies). Tune these down against the per-phase timings the apply
# marker records, not by guesswork.
_HEALTH_ATTEMPTS = 240
_HEALTH_INTERVAL_SECONDS = 1.0
_PREFLIGHT_ATTEMPTS = 240
_PREFLIGHT_INTERVAL_SECONDS = 1.0
_FRONTEND_PROBE_ATTEMPTS = 5
_FRONTEND_PROBE_INTERVAL_SECONDS = 1.0
_PREFLIGHT_OUTPUT_TAIL_LINES = 40

# Per-step wall-clock budgets for the forward apply steps. Nothing about an
# update should take anywhere near an hour, yet the old reveal ran for 1h28m
# before anyone asked whether it was stuck -- so a hung step (an `npm ci`
# stalled under load, a provisioner download that never completes) has to
# become a rollback with a named phase rather than an open-ended wait. Sized
# generously; the per-phase timings the marker records are the input for
# tuning them down. The rollback and recovery paths carry no budgets: there is
# no further rollback to absorb a timeout there.
_NPM_CI_TIMEOUT_SECONDS = 1200.0
_FRONTEND_BUILD_TIMEOUT_SECONDS = 1200.0
_ENVIRONMENT_REFRESH_TIMEOUT_SECONDS = 1200.0
_PROVISIONER_TIMEOUT_SECONDS = 1800.0
_RESTART_TIMEOUT_SECONDS = 600.0
_ENV_CONVERGE_TIMEOUT_SECONDS = 1200.0

# ``recover --if-stale``'s default grace: how long a marker must have gone
# without an update (with its process dead) before the cron path rolls the
# apply back. Long enough that a DRI agent re-running the idempotent ``apply``
# right after a kill wins the race; short enough that a workspace does not sit
# half-applied for long when nobody is coming back.
DEFAULT_RECOVER_GRACE_SECONDS = 600.0

# The ``oom_priority`` bands module, when the tree carries it. Loaded lazily
# from the target repo root (this script may run as a staged copy far from any
# in-tree package) and guarded, so the staged copy still runs on trees that
# predate the package. ``None`` means no banding and no expendable tagging.
_BANDS = None


def _load_bands(repo_root: Path):
    """Import ``oom_priority.bands`` from ``repo_root``'s tree, or ``None``.

    Deliberately not a module-level import: the apply is staged and executed
    from ``data/.tasks/update-self/skill-at-target/...``, so the package can
    only be found relative to the repo being applied to -- and an older tree
    may not carry it at all, which must degrade to "no banding" rather than a
    crash (the staged copy runs against older pre-merge trees by design).
    """
    bands_src = repo_root / "system" / "services" / "oom_priority" / "src"
    if not (bands_src / "oom_priority" / "bands.py").is_file():
        return None
    sys.path.insert(0, str(bands_src))
    try:
        from oom_priority import bands
    except ImportError as exc:
        # Distinct from the expected "this tree predates the package" case the
        # check above covers: the module is right there and would not import,
        # so the apply is about to run unbanded and nobody would know.
        sys.stderr.write(
            f"warning: {bands_src} carries an oom_priority package that could not be "
            f"imported ({exc}); this apply runs unprotected from memory shedding.\n"
        )
        return None
    finally:
        sys.path.remove(str(bands_src))
    return bands


def _protect_from_memory_shed(repo_root: Path) -> None:
    """Band this process into the near-exempt update-apply band.

    The apply orchestrator must outlive every agent, chat, and ordinary
    service: losing a build is an ordinary failure the rollback absorbs, but
    losing the apply mid-motion is the half-applied state this design exists to
    prevent. Only the authority paths that would repair a failed apply
    (owner-exec, the terminal) stay below it. Best-effort by construction; an
    apply that cannot be protected is still an apply worth running. Called from
    ``__main__`` rather than from the command functions so exercising them in a
    test cannot re-band the test runner.
    """
    global _BANDS
    _BANDS = _load_bands(repo_root)
    if _BANDS is None:
        return
    band = getattr(_BANDS, "UPDATE_APPLY", None)
    if band is None:
        # An older tree's bands module predates the update-apply band: use the
        # system interface's own service band, which the pre-apply reveal
        # flow this generalizes used for the same reason.
        band = _BANDS.SERVICE_BANDS.get("system_interface", 20)
    if not _BANDS.set_oom_score_adj(os.getpid(), band):
        sys.stderr.write(
            "warning: could not lower this process's memory-shed priority; a shed "
            "during the apply would skip the rollback.\n"
        )


# A step wrapper: takes the command to run and returns the argv to actually
# spawn. The forward apply hands its memory-hungry steps ``as_expendable``; the
# rollback/recover paths hand them the identity, so nothing there sheds.
ExpendWrapper = Callable[[Sequence[str]], list[str]]


def keep_protected(argv: Sequence[str]) -> list[str]:
    """The identity wrapper: the command inherits the orchestrator's band."""
    return list(argv)


def as_expendable(argv: Sequence[str]) -> list[str]:
    """Wrap ``argv`` so it runs in the most expendable band rather than this one.

    For the forward steps that actually hold memory -- ``npm ci`` / ``npm run
    build``, the uv installs, the pre-flight boot. Losing one of those is a
    failure this script recovers from; losing this script is not, so the
    protection it gives itself must not reach them by inheritance. Never used
    on the rollback/recover paths: there is no further rollback to absorb a
    shed there, so every recovery step keeps the orchestrator's protection.

    A no-op passthrough when the tree carries no ``oom_priority`` package.
    """
    if _BANDS is None:
        return list(argv)
    prefix = _BANDS.oom_tag_shell_prefix(_BANDS.AGENT_SUBPROCESS) + 'exec "$@"'
    return ["sh", "-c", prefix, "sh", *argv]


class ApplyError(Exception):
    """Base class for apply failures (avoids raising built-in exceptions)."""


class ApplyPreconditionError(ApplyError):
    """A precondition was not met; nothing was changed, do not roll back."""


class ApplyFailed(ApplyError):
    """A forward apply step failed; the caller must roll the merge back.

    ``live_service_restarted`` records whether the live services agent was
    already (re)started before the failure -- recovery restarts only then, so a
    failure before the restart never blips a UI that is still serving
    known-good code. ``detail`` is captured output explaining the failure (the
    pre-flight boot's own log); stderr gets all of it, the rollback commit only
    :meth:`headline`.
    """

    def __init__(
        self,
        message: str,
        *,
        live_service_restarted: bool = False,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.live_service_restarted = live_service_restarted
        self.detail = detail

    def headline(self) -> str:
        """The message plus only the last line of ``detail`` (the payload --
        a traceback ends on the exception that names the cause)."""
        last = next(
            (
                line
                for line in reversed(self.detail.strip().splitlines())
                if line.strip()
            ),
            "",
        )
        return f"{self}: {last}" if last else str(self)


class Runner:
    """Indirection over ``subprocess.run`` so tests can intercept commands."""

    def run(self, argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), **kwargs)

    def which(self, executable: str) -> str | None:
        """Resolve ``executable`` on PATH, as the shell running us would."""
        return shutil.which(executable)


@dataclass(frozen=True)
class FetchedPage:
    """A fetched response body plus the headers the frontend probe reads."""

    status: int
    body: str
    headers: dict[str, str]

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


@dataclass(frozen=True)
class FrontendProbe:
    """What the live UI said when asked whether it serves a working frontend."""

    failure: str | None
    is_answered: bool


class HttpClient:
    """Indirection over the loopback probes: the health checks (live service +
    pre-flight boot) and the frontend probe's page fetches."""

    def get_status(self, url: str, timeout: float) -> int | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, OSError):
            return None

    def get_page(self, url: str, timeout: float) -> FetchedPage | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                return FetchedPage(
                    status=int(response.status), body=body, headers=headers
                )
        except urllib.error.HTTPError as exc:
            return FetchedPage(status=int(exc.code), body="", headers={})
        except (urllib.error.URLError, OSError):
            return None


@dataclass
class Spawned:
    """A handle to a spawned throwaway server process."""

    _process: subprocess.Popen
    _output_path: Path

    def terminate(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def has_exited(self) -> bool:
        return self._process.poll() is not None

    def read_output(self) -> str:
        try:
            return self._output_path.read_text(errors="replace")
        except OSError:
            return ""


class Spawner:
    """Indirection over ``subprocess.Popen`` for the pre-flight throwaway boot.

    The child's stdout and stderr go to ``output_path`` rather than a pipe: a
    pipe whose buffer filled would block the very boot we are timing.
    """

    def spawn(
        self, argv: Sequence[str], cwd: str, env: dict, output_path: Path
    ) -> Spawned:
        with output_path.open("wb") as output_file:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=env,
                stdout=output_file,
                stderr=subprocess.STDOUT,
            )
        return Spawned(_process=process, _output_path=output_path)


# --- Apply planning ----------------------------------------------------------


def _is_backend_manifest(path: str) -> bool:
    """Whether ``path`` can change what the backend's environment resolves to.

    Not just the app's own manifest: the backend imports the vendored mngr and
    shells out to it, both as editable installs, so a vendored package's
    ``pyproject.toml`` moves their dependency closure exactly as the app's own
    does. Both workspace roots count; the vendored root is the one ``uv tool
    install -e system/vendor/mngr/libs/mngr`` resolves through.
    """
    if path in (
        f"{APP_DIR}/pyproject.toml",
        "uv.lock",
        "pyproject.toml",
        f"{MNGR_VENDOR_DIR}/pyproject.toml",
    ):
        return True
    parts = path.split("/")
    return (
        len(parts) == 6
        and parts[:3] == ["system", "vendor", "mngr"]
        and parts[3] == "libs"
        and parts[5] == "pyproject.toml"
    )


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith("_test.py") or name.startswith("test_")


class ApplyPlan(NamedTuple):
    """What one apply must do, derived from the merged diff's paths.

    The system-interface split (frontend vs backend, source vs manifest) is
    finer than :func:`classify_path`'s single ``system_interface`` class,
    because those four need different work; the rest rides on it --
    ``provisioner`` for the pinned-toolchain re-run and ``requires_restart``
    for the paths a live process must be bounced over (vendored-mngr source,
    ``.mngr/settings.toml``, the service class).
    """

    frontend_src: bool
    frontend_manifest: bool
    backend_src: bool
    backend_manifest: bool
    provisioner: bool
    requires_restart: bool

    @property
    def frontend(self) -> bool:
        return self.frontend_src or self.frontend_manifest

    @property
    def backend(self) -> bool:
        return self.backend_src or self.backend_manifest

    @property
    def needs_restart(self) -> bool:
        """Whether the services agent must restart before the workspace is
        consistent with the merged tree. The system interface's backend implies
        it; so does any ``requires_restart``-classified path."""
        return self.backend or self.requires_restart

    @property
    def any(self) -> bool:
        return (
            self.frontend or self.backend or self.provisioner or self.requires_restart
        )


def plan_apply(paths: Sequence[str]) -> ApplyPlan:
    """Classify the merged diff's ``paths`` into an :class:`ApplyPlan`.

    The frontend build output (``static/``) and ``node_modules`` are gitignored
    and never appear in a diff; they are covered by snapshots, not the plan.
    """
    frontend_src = False
    frontend_manifest = False
    backend_src = False
    backend_manifest = False
    provisioner = False
    requires_restart = False
    for path in paths:
        info = classify_path(path)
        if info.requires_restart:
            requires_restart = True
        if info.reveal_class == CLASS_PROVISIONER:
            provisioner = True
        if path in (
            f"{FRONTEND_DIR}/package.json",
            f"{FRONTEND_DIR}/package-lock.json",
        ):
            frontend_manifest = True
        elif path.startswith(f"{FRONTEND_DIR}/"):
            # Everything under frontend/ counts, not just src/: index.html, the
            # vite and TypeScript configs and the public assets all change the
            # emitted bundle.
            frontend_src = True
        elif _is_backend_manifest(path):
            backend_manifest = True
        elif (
            path.startswith(f"{APP_DIR}/imbue/")
            and path.endswith(".py")
            and not _is_test_file(path)
        ):
            backend_src = True
    return ApplyPlan(
        frontend_src=frontend_src,
        frontend_manifest=frontend_manifest,
        backend_src=backend_src,
        backend_manifest=backend_manifest,
        provisioner=provisioner,
        requires_restart=requires_restart,
    )


# --- The apply marker --------------------------------------------------------


@dataclass
class SnapshotRecord:
    """One pre-apply copy: what was copied and where the copy lives.

    ``source`` is the original absolute path (the restore destination);
    ``copy`` the absolute path of the pre-apply copy. Restores are plain file
    copies back to ``source`` -- no network, no package manager.
    """

    name: str
    source: str
    copy: str


@dataclass
class ApplyMarker:
    """The full-information record of an in-flight apply.

    Written before the merge lands and cleared on every exit path, so its
    presence *is* the interruption signal: boot-time recovery, the recovery
    cron, and the system interface's "an update was interrupted" banner all key
    off it, and a concurrent ``apply`` refuses to start while a live one
    exists. It carries everything a dependency-free rollback needs -- the
    rollback point, the snapshot manifest, whether the provisioner ran and
    whether the live service was restarted -- plus the DRI agent to re-engage
    afterwards.
    """

    dri_agent: str
    rollback_to: str
    merge_ref: str
    target_ref: str | None
    ff_only: bool
    worker_bundle: str | None
    phase: str
    pid: int
    started_at: float
    updated_at: float
    provisioner_ran: bool = False
    live_service_restarted: bool = False
    # Whether a working frontend was being served when the apply began -- the
    # regression baseline the probes hold the apply to. Persisted so a resumed
    # apply keeps the original baseline rather than re-measuring a workspace
    # its own interrupted run may have broken. ``None`` = not yet measured.
    frontend_expected: bool | None = None
    snapshots: list[SnapshotRecord] = field(default_factory=list)
    # When each phase was reached (epoch seconds), so every apply yields
    # per-phase durations and an interrupted one names the phase it hung in.
    phase_timings: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "dri_agent": self.dri_agent,
            "rollback_to": self.rollback_to,
            "merge_ref": self.merge_ref,
            "target_ref": self.target_ref,
            "ff_only": self.ff_only,
            "worker_bundle": self.worker_bundle,
            "phase": self.phase,
            "pid": self.pid,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "provisioner_ran": self.provisioner_ran,
            "live_service_restarted": self.live_service_restarted,
            "frontend_expected": self.frontend_expected,
            "phase_timings": dict(self.phase_timings),
            "snapshots": [
                {"name": s.name, "source": s.source, "copy": s.copy}
                for s in self.snapshots
            ],
        }
        return json.dumps(payload, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ApplyMarker":
        raw = json.loads(text)
        return cls(
            dri_agent=str(raw.get("dri_agent", "")),
            rollback_to=str(raw["rollback_to"]),
            merge_ref=str(raw["merge_ref"]),
            target_ref=raw.get("target_ref"),
            ff_only=bool(raw.get("ff_only", False)),
            worker_bundle=raw.get("worker_bundle"),
            phase=str(raw.get("phase", PHASE_STARTED)),
            pid=int(raw.get("pid", 0)),
            started_at=float(raw.get("started_at", 0.0)),
            updated_at=float(raw.get("updated_at", 0.0)),
            provisioner_ran=bool(raw.get("provisioner_ran", False)),
            live_service_restarted=bool(raw.get("live_service_restarted", False)),
            frontend_expected=raw.get("frontend_expected"),
            phase_timings={
                str(phase): float(at)
                for phase, at in (raw.get("phase_timings") or {}).items()
            },
            snapshots=[
                SnapshotRecord(
                    name=str(s["name"]), source=str(s["source"]), copy=str(s["copy"])
                )
                for s in raw.get("snapshots", [])
            ],
        )


def marker_path(repo_root: Path) -> Path:
    return repo_root / STATE_DIR_REL / MARKER_FILENAME


def _snapshots_root(repo_root: Path) -> Path:
    return repo_root / STATE_DIR_REL / SNAPSHOTS_DIRNAME


def read_marker(repo_root: Path) -> ApplyMarker | None:
    """Read the in-flight apply marker, or ``None`` when there is none.

    An unreadable or unparseable marker file reads as ``None`` plus a warning:
    every caller of this is deciding whether recovery work exists, and a
    corrupt marker must not wedge that decision forever -- the clean-tree and
    ancestor checks still guard the actual mutations.
    """
    path = marker_path(repo_root)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        sys.stderr.write(f"warning: could not read {path} ({exc}); ignoring it.\n")
        return None
    try:
        return ApplyMarker.from_json(text)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        sys.stderr.write(
            f"warning: {path} is not a valid marker ({exc}); ignoring it.\n"
        )
        return None


def write_marker(
    marker: ApplyMarker, repo_root: Path, now: Callable[[], float]
) -> None:
    """Persist ``marker`` atomically (write-then-rename), stamping ``updated_at``.

    Atomic so a reader (the recovery cron, the banner) never sees a torn file,
    and so a kill between write and rename leaves the previous state rather
    than none.
    """
    marker.updated_at = now()
    path = marker_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".json.tmp")
    scratch.write_text(marker.to_json())
    scratch.replace(path)


def clear_marker(repo_root: Path) -> None:
    marker_path(repo_root).unlink(missing_ok=True)


def emergency_path(repo_root: Path) -> Path:
    return repo_root / STATE_DIR_REL / EMERGENCY_FILENAME


def write_emergency(
    repo_root: Path, reason: str, dri_agent: str, now: Callable[[], float]
) -> None:
    """Record that a rollback left the workspace unhealthy, atomically.

    ``dri_agent`` comes from the marker being cleared, never from this
    process's environment: the paths that reach here unattended (the recovery
    cron, bootstrap) carry no ``MNGR_AGENT_NAME`` at all, and an agent-driven
    ``recover`` carries the *recovering* agent rather than the one whose apply
    failed. The marker is the only other place that name lives and it comes
    down on this same path.

    Best-effort: this runs on the way out of a failure that has already been
    written to stderr in full, so a filesystem that will not take the record
    must not turn a reported emergency into a traceback.
    """
    path = emergency_path(repo_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix(".json.tmp")
        scratch.write_text(
            json.dumps(
                {
                    "reason": reason,
                    "recorded_at": now(),
                    "dri_agent": dri_agent,
                    "snapshots_dir": str(_snapshots_root(repo_root)),
                },
                indent=2,
            )
        )
        scratch.replace(path)
    except OSError as exc:
        sys.stderr.write(
            f"warning: could not record the emergency at {path} ({exc}).\n"
        )


def clear_emergency(repo_root: Path) -> None:
    """Drop the emergency record; the live workspace is confirmed healthy again.

    Call only from an outcome that actually confirmed that -- the frontend
    included. A backend answering over a UI that is still down is not it: a
    broken UI is the usual aftermath of the failure that wrote the record, so
    clearing on the backend alone would take the banner away from the one
    workspace that still needs it.
    """
    emergency_path(repo_root).unlink(missing_ok=True)


def provision_incomplete_path(repo_root: Path) -> Path:
    return repo_root / STATE_DIR_REL / PROVISION_INCOMPLETE_FILENAME


def write_provision_incomplete(
    repo_root: Path,
    reason: str,
    dri_agent: str,
    merge_ref: str,
    now: Callable[[], float],
) -> None:
    """Record that an update landed with its provisioner run failed, atomically.

    Best-effort like the emergency record: the failure is already on stderr in
    full, and a filesystem that will not take the record must not turn a
    landed update into a traceback.
    """
    path = provision_incomplete_path(repo_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix(".json.tmp")
        scratch.write_text(
            json.dumps(
                {
                    "reason": reason,
                    "recorded_at": now(),
                    "dri_agent": dri_agent,
                    "merge_ref": merge_ref,
                    "provisioner": PROVISIONER_SCRIPT,
                },
                indent=2,
            )
        )
        scratch.replace(path)
    except OSError as exc:
        sys.stderr.write(
            f"warning: could not record the incomplete provisioning at {path} ({exc}).\n"
        )


def clear_provision_incomplete(repo_root: Path) -> None:
    """Drop the record; a provisioner run has completed cleanly."""
    provision_incomplete_path(repo_root).unlink(missing_ok=True)


def provisioner_env() -> dict:
    """The canonical environment for a live provisioner run (see
    :data:`_PROVISIONER_HOME`)."""
    env = dict(os.environ)
    env["HOME"] = _PROVISIONER_HOME
    env["PATH"] = _PROVISIONER_PATH
    return env


def _run_provisioner(runner: Runner, repo_root: Path) -> str | None:
    """Re-run the pinned-toolchain provisioner live; return why it failed, or
    ``None`` on success.

    Never raises: the forward apply carries on past a failed provisioner (a
    failed tool install leaves the tree and services consistent; whether the
    update is good is what the probes decide) and records the failure instead,
    so the caller needs the reason, not an exception.
    """
    try:
        result = runner.run(
            ["bash", PROVISIONER_SCRIPT],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            env=provisioner_env(),
            timeout=_PROVISIONER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return (
            f"bash {PROVISIONER_SCRIPT} did not finish within "
            f"{_PROVISIONER_TIMEOUT_SECONDS:g}s (hung or stalled)"
        )
    returncode = getattr(result, "returncode", 0)
    if returncode == 0:
        return None
    stderr = _tail((getattr(result, "stderr", "") or "").strip(), 20)
    return f"bash {PROVISIONER_SCRIPT} failed (exit {returncode}): {stderr}"


def _default_is_pid_a_live_apply(pid: int) -> bool:
    """Whether ``pid`` is alive and is an ``update_self.py`` process.

    The cmdline check (Linux ``/proc``; on hosts without it, liveness alone)
    guards against PID reuse: a recycled PID must not make a dead apply read as
    live forever.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # Alive, but owned by someone else.
    except OSError:
        return False
    cmdline_path = Path("/proc") / str(pid) / "cmdline"
    try:
        cmdline = (
            cmdline_path.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        )
    except OSError:
        return True  # No /proc (macOS): liveness is the best answer available.
    return "update_self" in cmdline


# --- Environment snapshots ---------------------------------------------------


def _tool_environment_dir(
    executable: str, tool_name: str, runner: Runner
) -> Path | None:
    """The installed tool environment behind ``executable``, or ``None``.

    Resolved from the console script's shebang (see :func:`_tool_location`), so
    the snapshot copies the installation actually being run rather than
    whatever uv would default to under this process's ``$HOME``.
    """
    found = runner.which(executable)
    location = _tool_location(Path(found), tool_name) if found is not None else None
    if location is None:
        return None
    return location[0] / tool_name


def snapshot_targets(
    plan: ApplyPlan, repo_root: Path, runner: Runner
) -> list[tuple[str, Path]]:
    """The state the apply's destructive steps can destroy, by plan.

    Every entry is a directory restored by a plain copy: the built bundle and
    ``node_modules`` (the build and ``npm ci`` both delete before they
    produce), the root venv (``uv sync`` rewrites it), and the two uv tool
    environments (``uv tool install --reinstall`` rebuilds them from scratch).
    """
    targets: list[tuple[str, Path]] = []
    if plan.frontend:
        targets.append(("bundle", repo_root / STATIC_DIR))
    if plan.frontend_manifest:
        targets.append(("node_modules", repo_root / FRONTEND_DIR / "node_modules"))
    if plan.backend_manifest:
        targets.append(("venv", repo_root / ".venv"))
        for tool_name, executable in (
            (MNGR_TOOL_NAME, MNGR_EXECUTABLE),
            (TOOL_NAME, TOOL_NAME),
        ):
            tool_dir = _tool_environment_dir(executable, tool_name, runner)
            if tool_dir is not None:
                targets.append((f"tool-{tool_name}", tool_dir))
    return targets


def take_snapshots(
    plan: ApplyPlan,
    repo_root: Path,
    runner: Runner,
    existing: Sequence[SnapshotRecord],
) -> list[SnapshotRecord]:
    """Copy aside everything the forward apply could destroy; return the manifest.

    ``existing`` is the marker's already-recorded manifest (a resumed apply):
    a copy that is still on disk is reused rather than re-taken, because by
    resume time the live state may already be part-destroyed -- re-copying it
    would overwrite the good copy with the wreckage.

    A target that does not exist contributes nothing (a workspace that never
    built a bundle has nothing to lose), and a copy that cannot be taken
    degrades to a warning rather than a refusal: this is a precaution, and
    recovery then falls back to rebuilding.
    """
    kept: list[SnapshotRecord] = [
        record for record in existing if Path(record.copy).exists()
    ]
    already = {record.name for record in kept}
    root = _snapshots_root(repo_root)
    for name, source in snapshot_targets(plan, repo_root, runner):
        if name in already:
            continue
        if not source.exists():
            sys.stderr.write(
                f"note: nothing to copy aside for '{name}' ({source} does not exist); "
                "a failed apply will have to rebuild it to recover.\n"
            )
            continue
        copy = root / name
        try:
            if copy.exists():
                shutil.rmtree(copy)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, copy, symlinks=True)
        except OSError as exc:
            shutil.rmtree(copy, ignore_errors=True)
            sys.stderr.write(
                f"warning: could not copy '{name}' aside ({type(exc).__name__}: {exc}); "
                "a failed apply will have to rebuild it to recover.\n"
            )
            continue
        kept.append(SnapshotRecord(name=name, source=str(source), copy=str(copy)))
    return kept


def restore_snapshots(snapshots: Sequence[SnapshotRecord]) -> list[str]:
    """Put every pre-apply copy back over its original path.

    Returns the names that could NOT be restored. Never raises: this is the
    last line of defense, and one failed restore must not stop the others.
    """
    failed: list[str] = []
    for record in snapshots:
        source = Path(record.source)
        copy = Path(record.copy)
        if not copy.exists():
            failed.append(record.name)
            sys.stderr.write(
                f"recovery: the copy of '{record.name}' at {copy} is gone; cannot restore it.\n"
            )
            continue
        try:
            if source.exists():
                shutil.rmtree(source)
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(copy, source, symlinks=True)
        except OSError as exc:
            failed.append(record.name)
            sys.stderr.write(
                f"recovery: could not restore '{record.name}' to {source} "
                f"({type(exc).__name__}: {exc}).\n"
            )
    return failed


def discard_snapshots(repo_root: Path) -> None:
    shutil.rmtree(_snapshots_root(repo_root), ignore_errors=True)


# --- Probes and helpers --------------------------------------------------------


def find_free_port() -> int:
    """Bind to an ephemeral port, then release it for the throwaway server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _diff_name_status(
    repo_root: Path, rollback_to: str, runner: Runner
) -> list[tuple[str, str]]:
    """Return ``(status, path)`` pairs for ``rollback_to..HEAD``.

    ``--no-renames`` makes a rename surface as a delete + add pair, which keeps
    the rollback logic simple (restore the deletes, remove the adds).
    """
    result = runner.run(
        ["git", "diff", "--no-renames", "--name-status", rollback_to, "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    pairs: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        pairs.append((fields[0].strip(), fields[-1].strip()))
    return pairs


def _assert_clean_tree(repo_root: Path, runner: Runner) -> None:
    result = runner.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip():
        raise ApplyPreconditionError(
            "working tree has uncommitted changes; refusing to apply so a rollback "
            "can never clobber unrelated work. Commit or stash, then re-run."
        )


def _abort_in_progress_merge(repo_root: Path, runner: Runner) -> bool:
    """Undo a ``git merge`` that was killed before it committed; report whether
    there was one.

    ``git merge`` writes ``MERGE_HEAD`` before it resolves anything and drops it
    only when the merge commit lands, so an apply killed anywhere inside its
    merge step leaves the merge *staged but uncommitted*: ``HEAD`` is still the
    rollback point, and the index holds the merged content. That state has to be
    undone before anything else commits, because git turns the next commit into
    the merge commit -- so the rollback's own commit would land the very merge it
    exists to undo, under a subject saying it was rolled back. (With conflicts
    still unresolved git refuses to commit at all, wedging every later recovery
    instead.) ``git merge --abort`` is a plain index/worktree reset back to
    ``HEAD``: no network, no package manager, exactly what the rollback is
    allowed to need.
    """
    merge_head = runner.run(
        ["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(merge_head, "returncode", 0) != 0:
        return False
    sys.stderr.write(
        "an interrupted merge is still staged (MERGE_HEAD is present); aborting it "
        "before restoring the tree.\n"
    )
    runner.run(
        ["git", "merge", "--abort"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return True


def _run_checked(
    runner: Runner,
    argv: Sequence[str],
    cwd: Path,
    what: str,
    *,
    live_service_restarted: bool = False,
    env: dict | None = None,
    timeout: float | None = None,
) -> None:
    """Run an apply command; raise :class:`ApplyFailed` on a non-zero exit, or
    when it outlives its ``timeout`` budget (a hung step is a failure with a
    name, not an open-ended wait)."""
    try:
        result = runner.run(
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ApplyFailed(
            f"{what} did not finish within {timeout:g}s (hung or stalled)",
            live_service_restarted=live_service_restarted,
        ) from None
    if getattr(result, "returncode", 0) != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise ApplyFailed(
            f"{what} failed (exit {result.returncode}): {stderr}",
            live_service_restarted=live_service_restarted,
        )


def wait_healthy(
    http: HttpClient,
    url: str,
    attempts: int,
    interval: float,
    sleeper: Callable[[float], None],
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Poll ``url`` until it returns HTTP 200, up to ``attempts`` times."""
    for index in range(attempts):
        if http.get_status(url, timeout=5.0) == 200:
            return True
        if should_stop is not None and should_stop():
            return False
        if index < attempts - 1:
            sleeper(interval)
    return False


def _detail_block(detail: str) -> str:
    return f"--- pre-flight boot output ---\n{detail}\n" if detail else ""


def _tail(text: str, limit: int) -> str:
    lines = text.strip().splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    dropped = len(lines) - limit
    return "\n".join([f"[{dropped} earlier line(s) omitted]", *lines[-limit:]])


def _preflight(
    repo_root: Path,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
    expend: ExpendWrapper = as_expendable,
) -> str | None:
    """Boot the merged backend on a throwaway port and probe it, without
    touching the live service. Returns ``None`` iff it serves a healthy
    response; otherwise the tail of what the throwaway boot wrote."""
    port = find_free_port()
    env = dict(os.environ)
    env["SYSTEM_INTERFACE_HOST"] = "127.0.0.1"
    env["SYSTEM_INTERFACE_PORT"] = str(port)
    # The caller is an agent, so its environment carries MNGR_AGENT_ID -- under
    # which the throwaway boot would persist layout state as if it were that
    # agent, clobbering the live layout.json. The preview flow
    # (reveal_system_interface.py) drops it for the same reason; the pre-flight
    # is just as much a throwaway boot and gets the same guard.
    env.pop("MNGR_AGENT_ID", None)
    with tempfile.TemporaryDirectory() as scratch:
        output_path = Path(scratch) / "preflight-boot.log"
        spawned = spawner.spawn(
            expend([TOOL_NAME]),
            cwd=str(repo_root / APP_DIR),
            env=env,
            output_path=output_path,
        )
        try:
            if wait_healthy(
                http,
                f"http://127.0.0.1:{port}{HEALTH_PATH}",
                _PREFLIGHT_ATTEMPTS,
                _PREFLIGHT_INTERVAL_SECONDS,
                sleeper,
                should_stop=spawned.has_exited,
            ):
                return None
        finally:
            spawned.terminate()
        return _tail(spawned.read_output(), _PREFLIGHT_OUTPUT_TAIL_LINES)


def probe_frontend(http: HttpClient, base_url: str) -> FrontendProbe:
    """Ask the live UI whether it is serving a working frontend.

    Asks the two questions a browser would -- is this the real app shell, and
    does its module script actually load as JavaScript -- which together cover
    both the missing-bundle state and the blank screen an unserved ``/assets``
    path produces.
    """
    shell = http.get_page(f"{base_url}{SERVE_PATH}", timeout=10.0)
    if shell is None:
        return FrontendProbe(
            "the live service did not answer a request for the app shell",
            is_answered=False,
        )
    if shell.status != 200:
        return FrontendProbe(
            f"the app shell returned HTTP {shell.status}", is_answered=True
        )
    if shell.headers.get(FRONTEND_BUILT_HEADER) == "false":
        return FrontendProbe(
            "the live service is serving the 'frontend not built' placeholder -- the compiled bundle is missing",
            is_answered=True,
        )
    match = _ASSET_REFERENCE_PATTERN.search(shell.body)
    if match is None:
        return FrontendProbe(
            "the app shell loads no bundled script, so it is not the built app",
            is_answered=True,
        )
    asset_url = f"{base_url}/assets/{match.group(1)}"
    asset = http.get_page(asset_url, timeout=10.0)
    if asset is None:
        return FrontendProbe(
            f"the live service did not answer a request for the bundled script {asset_url}",
            is_answered=False,
        )
    if asset.status != 200:
        return FrontendProbe(
            f"the bundled script {asset_url} returned HTTP {asset.status}",
            is_answered=True,
        )
    if "javascript" not in asset.content_type:
        return FrontendProbe(
            f"the bundled script {asset_url} came back as '{asset.content_type}' rather than JavaScript, "
            "so the browser will refuse it and render a blank page",
            is_answered=True,
        )
    return FrontendProbe(None, is_answered=True)


def _probe_frontend_until_answered(
    http: HttpClient, base_url: str, sleeper: Callable[[float], None]
) -> FrontendProbe:
    """:func:`probe_frontend`, retrying until the service actually answers.

    Only a *non-answer* is retried: a verdict -- the placeholder, a bad status,
    a script served as HTML -- is the service telling us the frontend really is
    broken, and asking again reaches the same conclusion more slowly.
    """
    probe = probe_frontend(http, base_url)
    for _ in range(_FRONTEND_PROBE_ATTEMPTS - 1):
        if probe.is_answered:
            return probe
        sleeper(_FRONTEND_PROBE_INTERVAL_SECONDS)
        probe = probe_frontend(http, base_url)
    return probe


def describe_frontend_failure(
    http: HttpClient, base_url: str, sleeper: Callable[[float], None]
) -> str | None:
    """Return why the live UI is not serving a working frontend, or ``None``."""
    return _probe_frontend_until_answered(http, base_url, sleeper).failure


def _refresh_workspace_view(repo_root: Path, runner: Runner) -> None:
    """Ask every open view of this workspace to reload the changed interface.

    Best-effort and never fatal: the change is already on disk and will load on
    the next visit regardless.
    """
    try:
        completed = runner.run(
            [sys.executable, str(repo_root / _REFRESH_SCRIPT)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_REFRESH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        sys.stderr.write(
            f"refresh: could not run {_REFRESH_SCRIPT} ({type(exc).__name__}: {exc}); "
            "an open view may still be showing the previous build until reloaded.\n"
        )
        return
    if completed.stderr:
        sys.stderr.write(completed.stderr)


def _tool_location(script: Path, tool_name: str) -> tuple[Path, Path] | None:
    """Return ``(tool_dir, bin_dir)`` for the uv tool that owns console
    ``script``, resolved from the script's shebang; ``None`` when it cannot be
    confirmed. Resolved from the shebang rather than asked of uv, for two
    reasons: uv's default tool dir follows ``$HOME``, which is not the one the
    workspace was built under, and a venv console script must not masquerade
    as a tool."""
    try:
        shebang = script.read_text(errors="replace").split("\n", 1)[0]
    except OSError:
        return None
    if not shebang.startswith("#!"):
        return None
    interpreter = shebang[2:].strip().split(" ", 1)[0]
    if not interpreter:
        return None
    parents = Path(interpreter).parents
    if len(parents) < 3:
        return None
    tool_dir = parents[2]
    if not (tool_dir / tool_name / _RECEIPT).is_file():
        return None
    return tool_dir, script.parent


def _uv_tool_env(executable: str, tool_name: str, runner: Runner) -> dict:
    """The environment for a ``uv tool`` call, aimed at ``executable``'s own
    installation when we can confirm which that is."""
    env = dict(os.environ)
    found = runner.which(executable)
    location = _tool_location(Path(found), tool_name) if found is not None else None
    if location is None:
        sys.stderr.write(
            f"refresh: could not identify the uv tool behind '{executable}'"
            f" ({found or 'not on PATH'}); letting uv choose the tool directory,"
            " which may rebuild a copy that is not the one being run.\n"
        )
        return env
    env["UV_TOOL_DIR"] = str(location[0])
    env["UV_TOOL_BIN_DIR"] = str(location[1])
    return env


def _tool_extras(
    tool_name: str, repo_root: Path, runner: Runner, env: dict
) -> list[str]:
    """Return the ``--with``/``--with-editable`` args a tool was installed with.

    A ``uv tool install --reinstall`` rebuilds the environment from the base
    package alone, dropping every extra -- for the mngr tool those extras *are*
    its plugins. uv records them in the tool's receipt; read them back rather
    than keeping a second copy of the plugin list to drift.
    """
    tool_dir = env.get("UV_TOOL_DIR")
    if tool_dir is None:
        result = runner.run(
            ["uv", "tool", "dir"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if getattr(result, "returncode", 0) != 0:
            _warn_extras_lost(tool_name, f"'uv tool dir' exited {result.returncode}")
            return []
        tool_dir = (getattr(result, "stdout", "") or "").strip()
    receipt = Path(tool_dir) / tool_name / _RECEIPT
    try:
        parsed = tomllib.loads(receipt.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        if receipt.is_file():
            _warn_extras_lost(tool_name, f"{receipt} is unreadable ({exc})")
        return []
    extras: list[str] = []
    for requirement in parsed.get("tool", {}).get("requirements", []):
        name = requirement.get("name", "")
        if _canonical(name) == _canonical(tool_name):
            continue  # the base package, which we re-pin to its in-tree source
        editable = requirement.get("editable") or requirement.get("directory")
        if editable:
            extras.extend(["--with-editable", editable])
        elif requirement.get("git"):
            extras.extend(["--with", f"{name} @ git+{requirement['git']}"])
        elif requirement.get("specifier"):
            extras.extend(["--with", f"{name}{requirement['specifier']}"])
        else:
            extras.extend(["--with", name])
    return extras


def _warn_extras_lost(tool_name: str, why: str) -> None:
    sys.stderr.write(
        f"refresh: cannot read what '{tool_name}' was installed with ({why}); "
        "reinstalling from the base package alone, which drops any plugins it "
        "had registered.\n"
    )


def _canonical(name: str) -> str:
    """Normalize a package name the way packaging does, for comparison."""
    return name.replace("_", "-").lower()


def _reinstall_tool(
    tool_name: str,
    executable: str,
    source_dir: str,
    repo_root: Path,
    runner: Runner,
    expend: ExpendWrapper,
    timeout: float | None = None,
) -> None:
    """Re-resolve the installed ``executable``'s tool from its in-tree source,
    keeping the extras it was installed with.

    ``expend`` gates the expendable tag: a forward install may be shed (the
    rollback restores the tool-environment snapshot), a recovery install must
    keep the orchestrator's protection (``keep_protected``).
    """
    env = _uv_tool_env(executable, tool_name, runner)
    argv = [
        "uv",
        "tool",
        "install",
        "-e",
        source_dir,
        *_tool_extras(tool_name, repo_root, runner, env),
        "--reinstall",
    ]
    _run_checked(
        runner,
        expend(argv),
        repo_root,
        f"uv tool install {tool_name} --reinstall",
        env=env,
        timeout=timeout,
    )


def _refresh_backend_dependencies(
    repo_root: Path,
    runner: Runner,
    expend: ExpendWrapper,
    timeout: float | None = None,
) -> None:
    """Re-resolve the three backend environments from the current tree,
    mirroring ``build_workspace.sh``: the vendored ``mngr`` tool, the
    ``system-interface`` tool, and the workspace venv (``uv sync``).
    ``timeout`` bounds each of the three (the forward apply's budget; recovery
    passes none)."""
    _reinstall_tool(
        MNGR_TOOL_NAME, MNGR_EXECUTABLE, MNGR_DIR, repo_root, runner, expend, timeout
    )
    _reinstall_tool(TOOL_NAME, TOOL_NAME, APP_DIR, repo_root, runner, expend, timeout)
    _run_checked(
        runner,
        expend(["uv", "sync", "--all-packages", "--frozen"]),
        repo_root,
        "uv sync --all-packages --frozen",
        timeout=timeout,
    )


def _restore_tree(
    name_status: Sequence[tuple[str, str]],
    rollback_to: str,
    repo_root: Path,
    runner: Runner,
) -> None:
    """Restore every changed path to its ``rollback_to`` state, staged for commit.

    Added-since paths are removed; modified/deleted paths are checked out from
    the known-good revision. Idempotent: re-running over an already-restored
    tree checks out and removes the same paths to the same state.
    """
    for status, path in name_status:
        if status.startswith("A"):
            runner.run(
                ["git", "rm", "--force", "--ignore-unmatch", path],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
            )
        else:
            runner.run(
                ["git", "checkout", rollback_to, "--", path],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=True,
            )


# The subject every rollback commit carries. Load-bearing, not cosmetic: the
# rollback is a *forward* revert, so the reverted merge stays in HEAD's
# ancestry and an ancestry check alone cannot tell "already applied" from
# "applied and undone". This prefix is how :func:`_has_rollback_since` tells
# them apart.
_ROLLBACK_SUBJECT_PREFIX = "Roll back update apply"


def _commit_rollback(
    repo_root: Path, runner: Runner, rollback_to: str, reason: str
) -> None:
    """Commit the staged restore as a forward revert, if there is anything to
    commit (a re-entered rollback may find the commit already landed)."""
    status = runner.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    if not status.stdout.strip():
        return
    message = f"{_ROLLBACK_SUBJECT_PREFIX} (restore to {rollback_to[:12]})\n\n{reason}"
    runner.run(
        ["git", "commit", "--no-verify", "-m", message],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )


# --- The version-history ledger ------------------------------------------------

_VERSION_HISTORY_REL = "docs/VERSION_HISTORY.md"
_WORKSPACE_HEADING = "## Workspace"
# Notes are padded to this width so the trailing shas line up; a note this wide
# or wider takes a plain two-space gap instead (``created from minds-v0.3.NN``
# is exactly 26 characters, so a bare pad would land the sha flush against it).
_LEDGER_NOTE_WIDTH = 26

# The canonical starter, recreated when the file was deleted since creation.
# Byte-identical to the ``docs/VERSION_HISTORY.md`` the template ships;
# ``publish-template``, ``update-published-template`` and
# ``update-installed-template`` all recreate it by reference to here.
_VERSION_HISTORY_STARTER = """\
# Version history

Where this workspace came from, what it has migrated in, what it has published,
and the templates it has adopted. Entries are appended automatically -- by
`update-self` when it lands a template update, by `migrate-workspace` when it
pulls another workspace in, by `publish-template` and
`update-published-template` when they publish, and by
`update-installed-template` when it pulls a newer version of an adopted
template -- and earlier lines are never rewritten. Each Workspace, Migrations,
and Templates line ends in the commit it was cut from.

## Workspace

## Migrations

## Templates

## Adopted templates

Each template this mind has adopted and the version it is on;
`update-installed-template` appends here when it pulls a newer version.
"""


def _ledger_line(date: str, note: str, sha: str) -> str:
    padded = note.ljust(_LEDGER_NOTE_WIDTH)
    if len(padded) - len(note) < 2:
        padded = note + "  "
    return f"- {date}  {padded}{sha}"


def _insert_under_workspace(lines: list[str], new_line: str, *, first: bool) -> None:
    """Insert ``new_line`` under ``## Workspace`` -- as the section's first line
    (the origin seed: the oldest event) or after its last existing line (an
    update entry). Existing lines are never re-flowed."""
    heading = lines.index(_WORKSPACE_HEADING)
    end = heading + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    entries = [index for index in range(heading + 1, end) if lines[index].strip() != ""]
    if first or not entries:
        position = entries[0] if entries else heading + 2
        # An empty section is ``## Workspace`` + a blank line; landing past the
        # section's end means the blank line was missing -- insert directly
        # after the heading instead.
        position = min(position, end)
    else:
        position = entries[-1] + 1
    lines.insert(position, new_line)
    # Keep a blank line between the entries and the next heading.
    after = position + 1
    if after < len(lines) and lines[after].startswith("## "):
        lines.insert(after, "")


def _git_out(runner: Runner, repo_root: Path, args: Sequence[str]) -> str:
    result = runner.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _origin_line(repo_root: Path, runner: Runner) -> str:
    """The one-time ``created from`` seed for ``## Workspace``.

    The template base is the OLDEST first-parent template-state marker (an
    ``update-self:`` merge or the ``Initial workspace commit``), falling back to
    the first-parent root; its date, version and sha come from that commit
    itself, so seeding late still records when the workspace was created. The
    version uses ``git describe`` (reachability), never ``--points-at``: no tag
    is ever *on* a template base, only on an ancestor of it.
    """
    log = _git_out(
        runner, repo_root, ["log", "--first-parent", "--format=%H %s", "HEAD"]
    )
    creation = ""
    for line in log.splitlines():
        sha, _, subject = line.partition(" ")
        if subject.startswith("update-self:") or subject == "Initial workspace commit":
            creation = sha  # keep walking: the log is newest-first, we want the oldest
    if not creation:
        revs = _git_out(runner, repo_root, ["rev-list", "--first-parent", "HEAD"])
        creation = revs.splitlines()[-1] if revs else "HEAD"
    date = _git_out(
        runner, repo_root, ["log", "-1", "--format=%ad", "--date=short", creation]
    )
    short = _git_out(runner, repo_root, ["rev-parse", "--short=7", creation])
    describe = runner.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "minds-v*", creation],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    version = (getattr(describe, "stdout", "") or "").strip()
    note = (
        f"created from {version}" if version else "created from the workspace template"
    )
    return _ledger_line(date, note, short)


def write_version_history_entry(
    repo_root: Path,
    runner: Runner,
    target_ref: str,
    merge_sha: str,
    today: str,
) -> None:
    """Record ``updated to <target_ref>`` in ``docs/VERSION_HISTORY.md``, committed.

    Landing an update is what makes the workspace a new version, so the entry
    belongs in the git tree as part of the same apply -- never left to a later
    turn. Append-only and idempotent: a ``## Workspace`` line already carrying
    this exact note and this exact 7-char sha means the update is recorded and
    nothing is written, so a resumed apply never duplicates it. The commit
    stages exactly this one file and must never carry an ``update-self:``
    subject (that prefix is the template-state marker the merge commit alone
    owns).
    """
    path = repo_root / _VERSION_HISTORY_REL
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_VERSION_HISTORY_STARTER)
    lines = path.read_text().splitlines()
    if _WORKSPACE_HEADING not in lines:
        # A hand-mangled file: append the section rather than losing the entry.
        lines.extend(["", _WORKSPACE_HEADING, ""])
    if not any("created from" in line for line in lines):
        _insert_under_workspace(lines, _origin_line(repo_root, runner), first=True)
    short = _git_out(runner, repo_root, ["rev-parse", "--short=7", merge_sha])
    note = f"updated to {target_ref}"
    if any(note in line and short in line for line in lines):
        return  # already recorded; a retried landing must be a no-op
    _insert_under_workspace(lines, _ledger_line(today, note, short), first=False)
    path.write_text("\n".join(lines) + "\n")
    runner.run(
        ["git", "add", _VERSION_HISTORY_REL],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    runner.run(
        ["git", "commit", "-m", f"version history: updated to {target_ref}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )


# --- The apply orchestration ---------------------------------------------------


def _is_merge_landed(merge_ref: str, repo_root: Path, runner: Runner) -> bool:
    """Whether ``merge_ref`` is already reachable from ``HEAD``."""
    result = runner.run(
        ["git", "merge-base", "--is-ancestor", merge_ref, "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    returncode = getattr(result, "returncode", 0)
    if returncode not in (0, 1):
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise ApplyPreconditionError(f"could not resolve {merge_ref}: {stderr}")
    return returncode == 0


def _has_rollback_since(merge_ref: str, repo_root: Path, runner: Runner) -> bool:
    """Whether a rollback commit sits between ``merge_ref`` and ``HEAD``.

    The one signal that distinguishes an already-*applied* merge from an
    already-*undone* one, both of which are ancestors of ``HEAD``: only the
    undone one has a :data:`_ROLLBACK_SUBJECT_PREFIX` commit on top of it.
    Scoped to ``merge_ref..HEAD``, and matching a subject this script itself
    writes, so ordinary workspace commits can never trip it.
    """
    log = _git_out(runner, repo_root, ["log", "--format=%s", f"{merge_ref}..HEAD"])
    return any(line.startswith(_ROLLBACK_SUBJECT_PREFIX) for line in log.splitlines())


def _expected_frontend_tree_hash(repo_root: Path, runner: Runner) -> str | None:
    """The merged tree's frontend-source tree hash, or ``None`` when git cannot
    answer (verification then degrades to the index-only check with a warning
    rather than blocking an apply over a read failure)."""
    result = runner.run(
        ["git", "rev-parse", f"HEAD:{FRONTEND_DIR}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(result, "returncode", 0) != 0:
        return None
    return (getattr(result, "stdout", "") or "").strip() or None


def _read_bundle_stamp(bundle_dir: Path) -> str | None:
    try:
        return (bundle_dir / BUNDLE_STAMP_FILENAME).read_text().strip() or None
    except OSError:
        return None


def _worker_bundle_reject_reason(
    worker_bundle: str | None, expected_hash: str | None
) -> str | None:
    """Why a ``--worker-bundle`` cannot be installed as-is, or ``None``.

    The stamp check is what keeps a stale-but-populated directory from being
    copied over the live UI while the source says otherwise -- the "source
    updated, UI didn't" state a user once had to catch by eye. A rejected
    bundle is not a failed apply: the live build remains the fallback, so the
    correct bundle is still produced (only the "what the user previewed is
    what ships" guarantee is lost, which the caller's note says).
    """
    if worker_bundle is None:
        return None
    source = Path(worker_bundle)
    if not (source / "index.html").exists():
        return "holds no built bundle (index.html missing)"
    if expected_hash is None:
        # Cannot verify (git could not resolve the merged frontend tree); the
        # index-only acceptance is all there is.
        return None
    stamp = _read_bundle_stamp(source)
    if stamp is None:
        return (
            f"carries no {BUNDLE_STAMP_FILENAME} stamp, so it cannot be verified "
            "against the merged source"
        )
    if stamp != expected_hash:
        return (
            f"was built from frontend source tree {stamp}, but the merged tree's "
            f"frontend is {expected_hash} -- it is stale"
        )
    return None


def _assert_bundle_built(
    repo_root: Path, expected_hash: str | None, *, live_service_restarted: bool
) -> None:
    """Raise unless the build actually left a servable bundle of the merged
    source behind.

    A build tool that empties its output directory and then exits 0 without
    writing passes an exit-code check while leaving nothing to serve -- and one
    that wrote nothing over a *populated* directory leaves an old bundle that
    serves fine while not matching the merged source at all. The stamp
    comparison catches the second case; it is skipped when ``expected_hash`` is
    ``None`` (recovery rebuilds on a rolled-back tree, where the pre-stamp
    build is normal) and degrades to a warning when the bundle simply carries
    no stamp (a build without a git repo writes none).
    """
    index = repo_root / FRONTEND_BUILD_INDEX
    if not index.exists():
        raise ApplyFailed(
            f"the frontend build reported success but wrote no bundle ({index} is missing)",
            live_service_restarted=live_service_restarted,
        )
    if expected_hash is None:
        return
    stamp = _read_bundle_stamp(repo_root / STATIC_DIR)
    if stamp is None:
        sys.stderr.write(
            f"note: the installed bundle carries no {BUNDLE_STAMP_FILENAME} stamp, "
            "so it could not be verified against the merged source.\n"
        )
        return
    if stamp != expected_hash:
        raise ApplyFailed(
            f"the installed bundle does not correspond to the merged source (built "
            f"from frontend tree {stamp}, merged tree is {expected_hash})",
            live_service_restarted=live_service_restarted,
        )


def _install_or_build_bundle(
    worker_bundle: str | None,
    repo_root: Path,
    runner: Runner,
    expend: ExpendWrapper,
    timeout: float | None = None,
) -> None:
    """Put the merged frontend's bundle in place.

    The worker's already-built ``static/`` is preferred when available -- it is
    the artifact the user previewed, and installing it is a plain copy that
    needs neither npm nor a registry. A live build is the fallback (the worker
    is gone, or its bundle path is wrong), tagged expendable: a shed build is
    an ordinary failure the rollback absorbs.
    """
    if worker_bundle is not None:
        source = Path(worker_bundle)
        if (source / "index.html").exists():
            destination = repo_root / STATIC_DIR
            try:
                if destination.exists():
                    shutil.rmtree(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, destination)
            except OSError as exc:
                raise ApplyFailed(
                    f"installing the worker's built bundle from {source} failed "
                    f"({type(exc).__name__}: {exc})"
                ) from exc
            return
        sys.stderr.write(
            f"note: --worker-bundle {source} holds no built bundle (index.html "
            "missing); building live instead.\n"
        )
    _run_checked(
        runner,
        expend(["npm", "run", "build"]),
        repo_root / FRONTEND_DIR,
        "npm run build",
        timeout=timeout,
    )


def _recover_running_state(
    plan: ApplyPlan,
    repo_root: Path,
    base_url: str,
    runner: Runner,
    http: HttpClient,
    sleeper: Callable[[float], None],
    *,
    live_service_restarted: bool,
    snapshots: Sequence[SnapshotRecord],
    is_frontend_expected: bool,
    provisioner_ran: bool,
) -> bool:
    """After the tree is restored to known-good, restore the pre-apply state and
    confirm the workspace is healthy. Returns True iff confirmed.

    Restores are file copies (no network, no package manager, no ``mngr``);
    rebuild/refresh fallbacks run only where there is no copy to put back. The
    provisioner re-run is best-effort by design: a failure (often no network)
    still counts as recovered, with the tools named as left ahead of the tree.
    Nothing here is tagged expendable -- there is no further rollback to absorb
    a shed. Never raises: this is the last line of defense, and the exit code
    is all the caller has to go on.
    """
    try:
        failed = set(restore_snapshots(snapshots))
        restored = {record.name for record in snapshots} - failed
        if provisioner_ran:
            result = runner.run(
                ["bash", PROVISIONER_SCRIPT],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
                env=provisioner_env(),
            )
            if getattr(result, "returncode", 0) != 0:
                sys.stderr.write(
                    "recovery: re-running the provisioner from the restored tree failed "
                    f"(exit {result.returncode}), so the globally pinned tools may be left "
                    "ahead of the tree. The rollback still counts as recovered -- re-run "
                    f"`bash {PROVISIONER_SCRIPT}` once the cause (often no network) is fixed.\n"
                )
        if plan.frontend and "bundle" not in restored:
            # No copy to put back: compile from source. node_modules likewise
            # has to match the restored lockfile when its own copy is gone.
            if plan.frontend_manifest and "node_modules" not in restored:
                _run_checked(runner, ["npm", "ci"], repo_root / FRONTEND_DIR, "npm ci")
            _run_checked(
                runner,
                ["npm", "run", "build"],
                repo_root / FRONTEND_DIR,
                "npm run build",
            )
            # No stamp comparison here: the tree is rolled back, and an older
            # tree's build may predate the stamping postbuild step.
            _assert_bundle_built(repo_root, None, live_service_restarted=False)
        if plan.backend_manifest and "venv" not in restored:
            _refresh_backend_dependencies(repo_root, runner, keep_protected)
        if live_service_restarted:
            _run_checked(
                runner,
                ["mngr", "start", "--restart", "system-services"],
                repo_root,
                "mngr start --restart",
            )
        if plan.needs_restart:
            healthy = wait_healthy(
                http,
                f"{base_url}{HEALTH_PATH}",
                _HEALTH_ATTEMPTS,
                _HEALTH_INTERVAL_SECONDS,
                sleeper,
            )
        else:
            healthy = wait_healthy(
                http,
                f"{base_url}{SERVE_PATH}",
                _HEALTH_ATTEMPTS,
                _HEALTH_INTERVAL_SECONDS,
                sleeper,
            )
    except (ApplyFailed, OSError) as exc:
        sys.stderr.write(f"recovery step failed: {exc}\n")
        return False
    if healthy and is_frontend_expected:
        frontend_failure = describe_frontend_failure(http, base_url, sleeper)
        if frontend_failure is not None:
            sys.stderr.write(f"recovery left the frontend broken: {frontend_failure}\n")
            return False
    if healthy:
        _refresh_workspace_view(repo_root, runner)
    return healthy


def _phase_timing_line(marker: ApplyMarker) -> str:
    """One stderr line of per-phase durations, from the marker's timings.

    The benchmarking input for tuning the poll and step budgets -- and, read
    from an interrupted apply's marker, what names the phase it hung in.
    """
    if not marker.phase_timings:
        return ""
    previous = marker.started_at
    parts: list[str] = []
    for phase, at in sorted(marker.phase_timings.items(), key=lambda item: item[1]):
        parts.append(f"{phase} +{at - previous:.1f}s")
        previous = at
    return "apply phase timings: " + ", ".join(parts) + "\n"


def _report_rolled_back(is_frontend_expected: bool) -> None:
    if is_frontend_expected:
        sys.stderr.write(
            "rolled back to last-known-good; the live workspace is confirmed healthy. "
            "The requested update did NOT land -- the worker branch and its report are "
            "kept, so once the failure is diagnosed a retry is a quick re-land.\n"
        )
    else:
        sys.stderr.write(
            "rolled back to last-known-good and the backend is healthy, but the live UI "
            "was not serving a working frontend before this apply either, so the "
            "rollback was not held to that standard and cannot confirm it. The requested "
            "update did NOT land -- diagnose both before retrying (the worker branch and "
            "its report are kept).\n"
        )


def _report_emergency(
    plan: ApplyPlan,
    repo_root: Path,
    reason: str,
    dri_agent: str,
    now: Callable[[], float],
) -> None:
    sys.stderr.write(
        "EMERGENCY: rollback did not restore a healthy workspace. The system interface "
        "may be down; manual intervention is required.\n"
    )
    # Durable, because stderr reaches whoever ran the apply and this state
    # outlives them: the banner reads this file, and so does the next agent.
    write_emergency(repo_root, reason, dri_agent, now)
    # The pre-apply copies outlive this failure on purpose: putting one back is
    # a plain file copy that needs neither npm nor a registry, so it is the way
    # out of exactly the failure that gets here. Only pointed at when the apply
    # touched the frontend -- after a backend-only apply the bundle copy is
    # byte-identical to what is already being served.
    bundle_copy = _snapshots_root(repo_root) / "bundle"
    if plan.frontend and bundle_copy.exists():
        sys.stderr.write(
            f"the pre-apply frontend bundle was kept at {bundle_copy} -- copying it over "
            f"{repo_root / STATIC_DIR} restores the UI without needing npm or a registry. "
            "Delete it once you have.\n"
        )


def apply_update(
    merge_ref: str,
    repo_root: Path,
    *,
    ff_only: bool,
    worker_bundle: str | None,
    target_ref: str | None,
    runner: Runner,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None] = time.sleep,
    base_url: str | None = None,
    now: Callable[[], float] = time.time,
    today: str | None = None,
    is_pid_live: Callable[[int], bool] = _default_is_pid_a_live_apply,
    expend: ExpendWrapper = as_expendable,
) -> int:
    """Land ``merge_ref`` and make the live workspace consistent with it, as one
    atomic, idempotent, rollback-on-failure motion. Returns the process exit
    code: 0 applied / 2 rolled back / 3 emergency / 1 precondition.

    Idempotent throughout: every phase checks current state before acting
    (merge already landed -> skip; snapshot already taken -> reuse; ledger
    entry present -> skip), so re-running ``apply`` after any interruption is
    safe -- that re-run *is* the DRI agent's recovery path.
    """
    resolved_base = (
        base_url or os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL)
    ).rstrip("/")

    # One in-flight apply at a time, keyed on the marker. A live marker with a
    # dead process is this apply's own interrupted predecessor: adopt it (same
    # merge), or send the caller to ``recover`` (a different merge).
    marker = read_marker(repo_root)
    if marker is not None:
        if marker.pid != os.getpid() and is_pid_live(marker.pid):
            sys.stderr.write(
                f"error: another apply is already running (pid {marker.pid}, started by "
                f"'{marker.dri_agent}'); refusing to interleave with it.\n"
            )
            return 1
        if marker.merge_ref != merge_ref:
            sys.stderr.write(
                f"error: an interrupted apply of a different merge ({marker.merge_ref}) "
                "left the workspace mid-motion; run "
                "`python3 .agents/skills/update-self/scripts/update_self.py recover` "
                "to roll it back before applying anything else.\n"
            )
            return 1
        sys.stderr.write(
            f"resuming the interrupted apply of {merge_ref} (last completed phase: "
            f"{marker.phase}).\n"
        )
        marker.pid = os.getpid()
        # The re-run's own flags win over the recorded ones -- the DRI agent
        # re-invokes with the same command, and a deliberate change (say a
        # corrected --worker-bundle path) must not be silently ignored.
        marker.ff_only = ff_only
        marker.target_ref = target_ref
        marker.worker_bundle = worker_bundle
        # A kill inside ``git merge`` leaves the merge staged but uncommitted.
        # That half-motion is this apply's own, so undo it and re-merge from a
        # clean tree rather than refusing on the dirt it left. Only here: on a
        # fresh apply an in-progress merge belongs to someone else, and the
        # clean-tree refusal below is the right answer.
        _abort_in_progress_merge(repo_root, runner)

    _assert_clean_tree(repo_root, runner)

    if marker is None:
        marker = ApplyMarker(
            dri_agent=os.environ.get(ENV_DRI_AGENT, ""),
            rollback_to=_git_out(runner, repo_root, ["rev-parse", "HEAD"]),
            merge_ref=merge_ref,
            target_ref=target_ref,
            ff_only=ff_only,
            worker_bundle=worker_bundle,
            phase=PHASE_STARTED,
            pid=os.getpid(),
            started_at=now(),
            updated_at=now(),
        )
    # Resolve the merge ref (read-only) BEFORE the marker is written: an
    # unresolvable ref raises the precondition error, and raising after the
    # write would leave a marker behind for an apply that never started --
    # showing the "update interrupted" banner and blocking other applies until
    # a needless `recover`. (A *resumed* apply's pre-existing marker survives
    # the raise, which is right: `recover` must still be able to roll it back.)
    is_merge_landed = _is_merge_landed(merge_ref, repo_root, runner)
    # A rolled-back merge is still an *ancestor* of HEAD -- the rollback is a
    # forward revert -- so without this an apply of it would skip the merge,
    # find an empty diff, and report "nothing live needed to change" plus a
    # version-history line for an update the tree does not contain. Re-running
    # the apply genuinely cannot re-land reverted content, so say so and stop.
    if is_merge_landed and _has_rollback_since(merge_ref, repo_root, runner):
        raise ApplyPreconditionError(
            f"{merge_ref} was landed and then rolled back, so its content is no "
            "longer in the tree even though the commit is still in history. "
            "Re-running the apply cannot re-land it: re-dispatch a fresh worker "
            "pass off the current HEAD instead. Nothing was changed."
        )
    write_marker(marker, repo_root, now)

    def _advance(phase: str) -> None:
        marker.phase = phase
        marker.phase_timings[phase] = now()
        write_marker(marker, repo_root, now)

    # --- Land the merge (skipped when already landed: idempotent re-entry). ---
    if not is_merge_landed:
        merge_argv = (
            ["git", "merge", "--ff-only", merge_ref]
            if ff_only
            else ["git", "merge", "--no-ff", "--no-edit", merge_ref]
        )
        result = runner.run(
            merge_argv, cwd=str(repo_root), capture_output=True, text=True, check=False
        )
        if getattr(result, "returncode", 0) != 0:
            # Nothing has landed: abort any half-merge, drop the marker, and
            # report as a precondition failure (exit 1, workspace untouched).
            runner.run(
                ["git", "merge", "--abort"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            clear_marker(repo_root)
            stderr = (getattr(result, "stderr", "") or "").strip()
            sys.stderr.write(
                f"error: merging {merge_ref} failed (exit {result.returncode}): {stderr}\n"
                "Nothing was changed. "
                + (
                    "A refused fast-forward means HEAD moved under the pass -- "
                    "re-dispatch off the current HEAD rather than hand-resolving.\n"
                    if ff_only
                    else "Resolve the conflict via a fresh worker pass rather than by hand.\n"
                )
            )
            return 1
    _advance(PHASE_MERGED)

    name_status = _diff_name_status(repo_root, marker.rollback_to, runner)
    plan = plan_apply([path for _, path in name_status])

    unresolved_frontend_failure: str | None = None
    provisioner_failure: str | None = None
    if plan.any:
        # The regression baseline: whether a working frontend is owed afterwards
        # is decided by what was being served *before* the apply -- measured
        # once and persisted, so a resumed apply is not judged against the
        # wreckage its own interrupted run left. Measured for every live plan
        # (a provisioner-only apply too): the rollback's recovery and its
        # report are held to this baseline, so leaving it unmeasured would
        # falsely report a healthy UI as already-broken after a rollback.
        if marker.frontend_expected is None:
            marker.frontend_expected = (
                describe_frontend_failure(http, resolved_base, sleeper) is None
            )
            write_marker(marker, repo_root, now)
        is_frontend_expected = bool(marker.frontend_expected)

        # Decide up front whether the worker's already-built bundle will be
        # installed: when it will, the npm dependency refresh below is dead
        # work on the critical path (installing the bundle is a plain copy that
        # needs no node_modules), and `npm ci` is the slowest, most
        # memory-hungry step of the whole motion. The stamp comparison is what
        # makes this decision trustworthy -- an unverifiable or stale bundle is
        # rejected here, so the live-build fallback (and its npm refresh) still
        # runs for it.
        expected_bundle_hash: str | None = None
        usable_worker_bundle: str | None = None
        if plan.frontend:
            expected_bundle_hash = _expected_frontend_tree_hash(repo_root, runner)
            bundle_reject = _worker_bundle_reject_reason(
                marker.worker_bundle, expected_bundle_hash
            )
            if marker.worker_bundle is not None:
                if bundle_reject is None:
                    usable_worker_bundle = marker.worker_bundle
                else:
                    sys.stderr.write(
                        f"note: --worker-bundle {marker.worker_bundle} "
                        f"{bundle_reject}; building live instead.\n"
                    )

        try:
            marker.snapshots = take_snapshots(plan, repo_root, runner, marker.snapshots)
            _advance(PHASE_SNAPSHOTTED)

            if plan.frontend_manifest and usable_worker_bundle is None:
                _run_checked(
                    runner,
                    expend(["npm", "ci"]),
                    repo_root / FRONTEND_DIR,
                    "npm ci",
                    timeout=_NPM_CI_TIMEOUT_SECONDS,
                )
            if plan.backend_manifest:
                _refresh_backend_dependencies(
                    repo_root, runner, expend, _ENVIRONMENT_REFRESH_TIMEOUT_SECONDS
                )
            _advance(PHASE_REFRESHED)

            # The provisioner runs before any restart, so nothing boots into a
            # tree whose pinned global toolchain has not caught up with it. Its
            # failure alone does not roll the merge back: a failed tool install
            # leaves the tree and services consistent, and re-running the
            # provisioner later is cheap and merge-independent -- whereas the
            # rollback costs the whole release plus a fresh worker pass. So the
            # apply carries on to the restart and the probes; a load-bearing
            # provisioner change (a node bump, a new apt dependency) still
            # fails those and still rolls back, and a landed update records
            # the gap (``write_provision_incomplete``) rather than hiding it.
            if plan.provisioner:
                # Recorded before the run is attempted, like the restart flag:
                # a provisioner that fails part-way (or is killed) may already
                # have moved global tool state, so recovery must re-run it from
                # the restored tree (best-effort) even then.
                marker.provisioner_ran = True
                write_marker(marker, repo_root, now)
                provisioner_failure = _run_provisioner(runner, repo_root)
                if provisioner_failure is not None:
                    sys.stderr.write(
                        f"warning: {provisioner_failure}\nContinuing without rolling "
                        "back: the tree and services stay consistent without the "
                        "provisioner, so the update lands if the probes pass and is "
                        "recorded as provisioning-incomplete; if they fail it rolls "
                        "back as usual.\n"
                    )
                _advance(PHASE_PROVISIONED)

            if plan.frontend:
                _install_or_build_bundle(
                    usable_worker_bundle,
                    repo_root,
                    runner,
                    expend,
                    _FRONTEND_BUILD_TIMEOUT_SECONDS,
                )
                _assert_bundle_built(
                    repo_root, expected_bundle_hash, live_service_restarted=False
                )
                _advance(PHASE_BUILT)

            if plan.needs_restart:
                preflight_output = _preflight(repo_root, http, spawner, sleeper, expend)
                if preflight_output is not None:
                    raise ApplyFailed(
                        "merged backend failed to boot in a pre-flight check; live "
                        "service not restarted",
                        detail=preflight_output
                        or "(the pre-flight boot wrote nothing at all)",
                    )
                # Recorded before the restart is attempted, so a kill anywhere
                # past this line leaves a marker that tells recovery to restart.
                marker.live_service_restarted = True
                write_marker(marker, repo_root, now)
                _run_checked(
                    runner,
                    ["mngr", "start", "--restart", "system-services"],
                    repo_root,
                    "mngr start --restart",
                    live_service_restarted=True,
                    timeout=_RESTART_TIMEOUT_SECONDS,
                )
                _advance(PHASE_RESTARTED)
                if not wait_healthy(
                    http,
                    f"{resolved_base}{HEALTH_PATH}",
                    _HEALTH_ATTEMPTS,
                    _HEALTH_INTERVAL_SECONDS,
                    sleeper,
                ):
                    raise ApplyFailed(
                        "backend did not become healthy after restart",
                        live_service_restarted=True,
                    )

            if plan.frontend or plan.needs_restart:
                # Scoped to a *regression*: only a frontend that was serving
                # before this apply has to be serving after it. Ahead of the
                # view refresh, so an apply that regressed the frontend rolls
                # back rather than asking every open view to reload into it.
                unresolved_frontend_failure = describe_frontend_failure(
                    http, resolved_base, sleeper
                )
                if unresolved_frontend_failure is not None:
                    if is_frontend_expected:
                        raise ApplyFailed(
                            "the live UI stopped serving a working frontend: "
                            f"{unresolved_frontend_failure}",
                            live_service_restarted=plan.needs_restart,
                        )
                    sys.stderr.write(
                        "warning: the live UI is not serving a working frontend, and was "
                        "not before this apply either, so it was not rolled back for it: "
                        f"{unresolved_frontend_failure}\n"
                    )
            # Past the last rollback point: nothing after the probes can raise
            # ApplyFailed, so the interruption marker and the snapshots come
            # down NOW -- before the view refresh (a shell reloading into a
            # lingering marker would render the "update was interrupted"
            # banner over an apply that just succeeded) and before the
            # post-success bookkeeping (so an unattended ``recover`` can never
            # roll back an update that already went live; the ledger append
            # and env-converge are both safely re-runnable without a marker).
            sys.stderr.write(_phase_timing_line(marker))
            if provisioner_failure is not None:
                write_provision_incomplete(
                    repo_root, provisioner_failure, marker.dri_agent, merge_ref, now
                )
            elif plan.provisioner:
                clear_provision_incomplete(repo_root)
            clear_marker(repo_root)
            discard_snapshots(repo_root)
            # The emergency record only comes down on confirmed health, which
            # is more than this exit code carries: an apply over a UI that was
            # already broken lands, exits 0 naming the breakage, and leaves a
            # user who still cannot see the workspace -- exactly the state the
            # record exists to keep visible.
            if is_frontend_expected and unresolved_frontend_failure is None:
                clear_emergency(repo_root)
            _refresh_workspace_view(repo_root, runner)
        except ApplyFailed as exc:
            sys.stderr.write(
                f"apply failed: {exc}\n{_detail_block(exc.detail)}"
                f"{_phase_timing_line(marker)}"
                f"rolling back to {marker.rollback_to[:12]} and restoring the "
                "workspace...\n"
            )
            try:
                _restore_tree(name_status, marker.rollback_to, repo_root, runner)
                _commit_rollback(
                    repo_root,
                    runner,
                    marker.rollback_to,
                    f"Apply failed and was auto-reverted: {exc.headline()}",
                )
                is_recovered = _recover_running_state(
                    plan,
                    repo_root,
                    resolved_base,
                    runner,
                    http,
                    sleeper,
                    live_service_restarted=exc.live_service_restarted
                    or marker.live_service_restarted,
                    snapshots=marker.snapshots,
                    is_frontend_expected=is_frontend_expected,
                    provisioner_ran=marker.provisioner_ran,
                )
            except subprocess.CalledProcessError as rollback_exc:
                sys.stderr.write(f"the rollback itself failed: {rollback_exc}\n")
                is_recovered = False
            if is_recovered:
                clear_marker(repo_root)
                discard_snapshots(repo_root)
                # Same rule as the success path: the frontend is the half
                # ``_recover_running_state`` only probes when one was expected,
                # so that is the only case whose health it confirms.
                if is_frontend_expected:
                    clear_emergency(repo_root)
                _report_rolled_back(is_frontend_expected)
                return 2
            # The marker is cleared even on the emergency path: this is a
            # deliberate, fully-reported exit, and re-running the same failed
            # rollback from cron would not help. The snapshots are kept -- they
            # are the operator's way back, and so is the emergency record the
            # report writes in the marker's place.
            clear_marker(repo_root)
            _report_emergency(
                plan,
                repo_root,
                f"apply of {merge_ref} failed and its rollback could not restore "
                f"health: {exc.headline()}",
                marker.dri_agent,
                now,
            )
            return 3
    else:
        sys.stderr.write("nothing live needed to change for this merge.\n")
        # Same reasoning as the live-plan clear above: the merge is landed and
        # nothing live was (or will be) touched, so a kill during the
        # bookkeeping below must not leave a marker that reads as an update
        # worth rolling back.
        clear_marker(repo_root)
        discard_snapshots(repo_root)

    # --- Post-success bookkeeping (update-self mode only). -----------------------
    if target_ref is not None:
        # For the fast-forward landing the merge commit IS the worker branch's
        # tip, so the sha is re-derivable on any re-run -- which is what keeps
        # the ledger append a no-op after an interruption.
        try:
            merge_sha = _git_out(runner, repo_root, ["rev-parse", merge_ref])
            write_version_history_entry(
                repo_root,
                runner,
                target_ref,
                merge_sha,
                today or datetime.date.today().isoformat(),
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            sys.stderr.write(
                f"warning: the update landed but the version-history entry could not "
                f"be recorded ({exc}); record it manually per the update-self skill.\n"
            )
        # The one moment package versions are allowed to move. Post-success
        # only, so a failed apply never moved apt state; a failure here is
        # reported but does not un-apply the update.
        try:
            converge = runner.run(
                ["uv", "run", "env-converge", "upgrade"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
                timeout=_ENV_CONVERGE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            converge = subprocess.CompletedProcess(
                ["uv", "run", "env-converge", "upgrade"],
                returncode=124,
                stdout="",
                stderr=f"did not finish within {_ENV_CONVERGE_TIMEOUT_SECONDS:g}s",
            )
        if getattr(converge, "returncode", 0) != 0:
            stderr = (getattr(converge, "stderr", "") or "").strip()
            sys.stderr.write(
                f"warning: `uv run env-converge upgrade` failed (exit "
                f"{converge.returncode}): {stderr}\nThe update is applied; re-run it "
                "once the cause is fixed so the pinned apt snapshot advances.\n"
            )
        elif getattr(converge, "stdout", ""):
            sys.stdout.write(converge.stdout)

    if provisioner_failure is not None:
        sys.stderr.write(
            f"applied with incomplete provisioning: {provisioner_failure}\nThe update "
            "is landed and the live workspace is healthy, but the pinned global "
            f"toolchain did not catch up with the tree. Re-run `bash {PROVISIONER_SCRIPT}` "
            "once the cause is fixed; the gap is recorded at "
            f"{provision_incomplete_path(repo_root)} until a provisioner run succeeds.\n"
        )
    if unresolved_frontend_failure is not None:
        sys.stderr.write(
            "applied: the update landed and the backend is healthy, but the live UI is "
            "still not serving a working frontend: "
            f"{unresolved_frontend_failure}. That was already true before this apply, "
            "so it was not rolled back for it -- report it and diagnose it separately.\n"
        )
        return 0
    sys.stderr.write(
        "applied: the update is landed and the live workspace is confirmed healthy.\n"
        if plan.any
        else "applied: the update is landed (nothing live needed to change).\n"
    )
    return 0


def recover(
    repo_root: Path,
    *,
    if_stale: bool,
    grace_seconds: float,
    no_restart: bool,
    runner: Runner,
    http: HttpClient,
    sleeper: Callable[[float], None] = time.sleep,
    base_url: str | None = None,
    now: Callable[[], float] = time.time,
    is_pid_live: Callable[[int], bool] = _default_is_pid_a_live_apply,
) -> int:
    """Roll back an interrupted apply from its marker.

    ``--if-stale`` is the unattended guard (boot and cron): act only when a
    marker exists, its recorded process is dead, and it has gone ``grace``
    without an update -- and stay silent in every normal state, because the
    cron runs forever. Bare ``recover`` is the explicit agent-driven rollback.

    ``--no-restart`` is the boot path: nothing is running yet, so disk state is
    the whole job (bootstrap starts the services fresh from the restored tree)
    and the health probes would only time out against a server that has not
    booted. The marker survives a failed *tree restore* so the next pass
    retries it; a rollback that restored the tree but could not confirm a
    healthy workspace clears the marker before reporting the emergency, like
    the apply's own emergency path -- re-running the same failed rollback from
    cron would not help.
    """
    resolved_base = (
        base_url or os.environ.get(ENV_WORKSPACE_URL, DEFAULT_WORKSPACE_URL)
    ).rstrip("/")
    marker = read_marker(repo_root)
    if marker is None:
        if not if_stale:
            sys.stderr.write("no interrupted apply to recover (no marker found).\n")
        return 0
    if is_pid_live(marker.pid):
        if if_stale:
            return 0
        sys.stderr.write(
            f"error: the apply (pid {marker.pid}) is still running; refusing to roll "
            "back underneath it.\n"
        )
        return 1
    if if_stale and (now() - marker.updated_at) < grace_seconds:
        # Freshly dead: give the DRI agent its window to simply re-run the
        # idempotent apply before the unattended path rolls it back.
        return 0

    sys.stderr.write(
        f"recovering an interrupted apply of {marker.merge_ref} (last completed "
        f"phase: {marker.phase}, DRI agent: '{marker.dri_agent}'); rolling back to "
        f"{marker.rollback_to[:12]}...\n"
    )
    name_status = _diff_name_status(repo_root, marker.rollback_to, runner)
    plan = plan_apply([path for _, path in name_status])
    try:
        # Before anything commits: an apply killed inside its merge left the
        # merge staged, and committing on top of that would land it instead of
        # rolling it back.
        _abort_in_progress_merge(repo_root, runner)
        _restore_tree(name_status, marker.rollback_to, repo_root, runner)
        _commit_rollback(
            repo_root,
            runner,
            marker.rollback_to,
            f"Interrupted apply of {marker.merge_ref} (last completed phase: "
            f"{marker.phase}) rolled back by recover",
        )
    except subprocess.CalledProcessError as exc:
        # The marker is kept: the tree is still mid-motion and a later recover
        # (or the DRI agent) must be able to try again.
        sys.stderr.write(f"recover: restoring the tree failed: {exc}\n")
        return 1

    if no_restart:
        failed = restore_snapshots(marker.snapshots)
        if marker.provisioner_ran:
            result = runner.run(
                ["bash", PROVISIONER_SCRIPT],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
                env=provisioner_env(),
            )
            if getattr(result, "returncode", 0) != 0:
                sys.stderr.write(
                    "recover: re-running the provisioner from the restored tree failed "
                    f"(exit {result.returncode}); the globally pinned tools may be left "
                    "ahead of the tree.\n"
                )
        clear_marker(repo_root)
        if failed:
            # The copies stay, for the same reason the emergency path keeps
            # them: a restore that failed for anything other than a missing
            # copy (a full disk, a permission fault) leaves the copy sitting
            # right there, and putting it back by hand is the way out. Deleting
            # them here would destroy the only remaining route.
            sys.stderr.write(
                f"recover: could not restore: {', '.join(sorted(failed))}. The tree is "
                "rolled back but the pre-apply state is NOT -- the copies are kept at "
                f"{_snapshots_root(repo_root)}, so copying one back by hand is the "
                "quickest repair; whatever has no copy left has to be rebuilt. "
                "Services will boot against that mismatch.\n"
            )
            return 0
        discard_snapshots(repo_root)
        sys.stderr.write(
            "recovered: the tree and pre-apply state are rolled back; services will "
            "boot fresh from the restored state.\n"
        )
        return 0

    is_recovered = _recover_running_state(
        plan,
        repo_root,
        resolved_base,
        runner,
        http,
        sleeper,
        live_service_restarted=marker.live_service_restarted,
        snapshots=marker.snapshots,
        is_frontend_expected=bool(marker.frontend_expected),
        provisioner_ran=marker.provisioner_ran,
    )
    if is_recovered:
        clear_marker(repo_root)
        discard_snapshots(repo_root)
        # Same rule again, and it decides both what this clears and what it
        # may claim: the frontend is probed only when one was expected, so a
        # recovery of an apply that had no working UI to be held to has
        # confirmed the backend and nothing else. This line is often the only
        # account of an unattended recovery, so it must not sign off on a UI
        # nobody looked at -- and an apply killed before it measured its
        # baseline (the marker predates the merge, the baseline probe follows
        # it) has no observation of that UI to report at all.
        if marker.frontend_expected:
            clear_emergency(repo_root)
            confirmation = "the live workspace is confirmed healthy"
        else:
            unheld = (
                "the live UI was not serving a working frontend when that apply began "
                "either"
                if marker.frontend_expected is False
                else "that apply was killed before it recorded whether the live UI was "
                "serving a working frontend"
            )
            confirmation = (
                f"the backend is healthy, but {unheld}, so this rollback was not held "
                "to that standard and cannot confirm it"
            )
        sys.stderr.write(
            f"recovered: the interrupted apply is rolled back and {confirmation}. The "
            "worker branch and its report are kept, so a diagnosed retry is a quick "
            "re-land.\n"
        )
        return 0
    clear_marker(repo_root)
    _report_emergency(
        plan,
        repo_root,
        f"an interrupted apply of {marker.merge_ref} (last completed phase: "
        f"{marker.phase}) was rolled back, but the live workspace could not be "
        "confirmed healthy",
        marker.dri_agent,
        now,
    )
    return 1


def _cmd_apply(args: argparse.Namespace) -> int:
    return apply_update(
        args.merge_ref,
        _repo_root(args).resolve(),
        ff_only=args.ff_only,
        worker_bundle=args.worker_bundle,
        target_ref=args.target_ref,
        runner=Runner(),
        http=HttpClient(),
        spawner=Spawner(),
    )


def _cmd_recover(args: argparse.Namespace) -> int:
    return recover(
        _repo_root(args).resolve(),
        if_stale=args.if_stale,
        grace_seconds=args.grace_seconds,
        no_restart=args.no_restart,
        runner=Runner(),
        http=HttpClient(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    # ``--repo-root`` lives on a shared parent parser so it is accepted both
    # before and after the subcommand (an option defined only on the top-level
    # parser would reject ``update_self.py <subcommand> --repo-root X``).
    # The default must be ``SUPPRESS``, not a value: on Python < 3.13 a
    # subparser re-applies its defaults over the namespace the top-level parser
    # already filled in (bpo-9351), so a concrete default here would clobber a
    # ``--repo-root`` given before the subcommand. With ``SUPPRESS`` the
    # attribute is only set when the flag is actually passed; ``_repo_root``
    # falls back to cwd.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo-root",
        type=Path,
        default=argparse.SUPPRESS,
        help="Repo root the git subcommands run in (default: cwd).",
    )
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    resolve_parser = sub.add_parser(
        "resolve-target", help="Resolve the update target ref.", parents=[common]
    )
    resolve_parser.add_argument(
        "--override",
        default=None,
        help="A tag, 'main', or any ref to update to (default: latest stable "
        "minds-v* tag).",
    )
    resolve_parser.add_argument(
        "--remote", default="upstream", help="Remote to read tags from."
    )
    resolve_parser.add_argument(
        "--local-tags",
        action="store_true",
        help="Read already-fetched local tags instead of querying the remote.",
    )
    resolve_parser.add_argument(
        "--ceiling",
        default=None,
        help="Newest template ref to allow (default: ask the running minds app). "
        "A non-release ref (e.g. a branch) imposes no ceiling.",
    )
    resolve_parser.set_defaults(func=_cmd_resolve_target)

    classify_parser = sub.add_parser(
        "classify-merge",
        help="Split upstream-changed files into merged vs pulled-in and classify each.",
        parents=[common],
    )
    classify_parser.add_argument(
        "--target", required=True, help="The upstream ref being merged in."
    )
    classify_parser.add_argument(
        "--local",
        default="HEAD",
        help="The local ref (default HEAD; use HEAD^1 after the merge commit).",
    )
    classify_parser.add_argument(
        "--base",
        default=None,
        help="Merge base (default: git merge-base <local> <target>).",
    )
    classify_parser.set_defaults(func=_cmd_classify_merge)

    changelog_parser = sub.add_parser(
        "changelog-entries",
        help="List per-PR changelog entries newly added between two refs "
        "(across every project bucket, not just the top-level changelog/).",
        parents=[common],
    )
    changelog_parser.add_argument("--base", required=True, help="Base ref.")
    changelog_parser.add_argument("--target", required=True, help="Target ref.")
    changelog_parser.set_defaults(func=_cmd_changelog_entries)

    bootstrap_parser = sub.add_parser(
        "bootstrap-skill",
        help="Extract the target ref's own update-self skill into a staging dir "
        "and report whether it differs from the local copy.",
        parents=[common],
    )
    bootstrap_parser.add_argument(
        "--ref",
        required=True,
        help="The resolved target ref to extract the skill from.",
    )
    bootstrap_parser.add_argument(
        "--dest",
        default="data/.tasks/update-self/skill-at-target",
        help="Staging dir the skill is extracted into (default: "
        "data/.tasks/update-self/skill-at-target).",
    )
    bootstrap_parser.set_defaults(func=_cmd_bootstrap_skill)

    apply_parser = sub.add_parser(
        "apply",
        help="Land a prepared merge and make the live workspace consistent with "
        "it: one atomic, idempotent, rollback-on-failure motion (merge, "
        "snapshots, env refresh, provisioner, build, pre-flight, restart, "
        "probes, ledger, env-converge).",
        parents=[common],
    )
    apply_parser.add_argument(
        "--merge-ref",
        required=True,
        help="The worker branch / prepared merge commit to land.",
    )
    apply_parser.add_argument(
        "--ff-only",
        action="store_true",
        help="Require a fast-forward landing (the update-self flow; the worker "
        "branched off this HEAD). Default is an ordinary merge "
        "(update-system-interface).",
    )
    apply_parser.add_argument(
        "--worker-bundle",
        default=None,
        help="Path to the worker's already-built static/ bundle (the artifact "
        "the user previewed); a live build is the fallback.",
    )
    apply_parser.add_argument(
        "--target-ref",
        default=None,
        help="The release this update lands (update-self mode): enables the "
        "VERSION_HISTORY.md ledger entry and the post-success "
        "`env-converge upgrade`.",
    )
    apply_parser.set_defaults(func=_cmd_apply)

    recover_parser = sub.add_parser(
        "recover",
        help="Roll back an interrupted apply from its marker (dependency-free: "
        "git restore + snapshot copies).",
        parents=[common],
    )
    recover_parser.add_argument(
        "--if-stale",
        action="store_true",
        help="Unattended guard (boot/cron): act only when the marker's process "
        "is dead and the marker is older than the grace period; silently "
        "exit 0 in every normal state.",
    )
    recover_parser.add_argument(
        "--grace-seconds",
        type=float,
        default=DEFAULT_RECOVER_GRACE_SECONDS,
        help="How long a marker must have gone without an update before "
        "--if-stale acts (default: %(default)s).",
    )
    recover_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Boot path: restore disk state only, without service restarts or "
        "health probes (services boot fresh from the restored state).",
    )
    recover_parser.set_defaults(func=_cmd_recover)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (CeilingUnavailableError, NoUpdateTargetError, ApplyPreconditionError) as e:
        # These carry the "why you cannot update right now" explanation the lead
        # relays to the user, so print the message alone: a traceback would bury it
        # and read as a crash rather than a refusal.
        print(f"error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"error: git command failed: {e}", file=sys.stderr)
        return 1


def _shed_protection_target(argv: Sequence[str]) -> Path | None:
    """The repo root to band for when ``argv`` names apply/recover, else None.

    Only those two band themselves: they are the motions that can be
    interrupted half-way through replacing what the workspace runs, and the
    ones holding the only copies of what it ran before. A crude parse rather
    than argparse, because banding must happen before ``main`` does anything.
    """
    tokens = list(argv)
    repo_root = Path.cwd()
    subcommand: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--repo-root" and index + 1 < len(tokens):
            repo_root = Path(tokens[index + 1])
            index += 2
            continue
        if token.startswith("--repo-root="):
            repo_root = Path(token.split("=", 1)[1])
            index += 1
            continue
        if subcommand is None and not token.startswith("-"):
            subcommand = token
        index += 1
    if subcommand in ("apply", "recover"):
        return repo_root
    return None


if __name__ == "__main__":
    _banding_root = _shed_protection_target(sys.argv[1:])
    if _banding_root is not None:
        _protect_from_memory_shed(_banding_root.resolve())
    sys.exit(main())
