"""How a changed path is applied and validated: the change classes ``classify-merge``
reports to the worker, the provisioner's inputs, and the apply plan derived from a
merged diff.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Collection, NamedTuple, Sequence

from update_layout import (
    APPS_DIR,
    FRONTEND_SOURCE_DIRS,
    FRONTEND_TOOLING_PATHS,
    NPM_MANIFEST_PATHS,
    PLUGIN_MANIFEST_PATH,
    PROVISIONER_SCRIPT,
    PYPROJECT_PATH,
    SYSTEM_INTERFACE_DIR,
)

CLASS_SYSTEM_INTERFACE = "system_interface"

CLASS_SERVICE = "service"


CLASS_SHARED_RUNTIME = "shared_runtime"

CLASS_PROVISIONER = "provisioner"

CLASS_DOCKERFILE = "dockerfile"

CLASS_DOCS = "docs"

CLASS_OTHER = "other"


# The one file outside ``system/scripts/`` the provisioner reads: the committed
# apt snapshot timestamp, read by the ``write_apt_sources.sh`` it chains.
_APT_SNAPSHOT_TIMESTAMP = ".mngr/apt-snapshot-timestamp"

# A line on which the provisioner runs or sources another script by path
# (``bash "$dir/x.sh"``, ``. "$(dirname "$0")/x.sh"``), as opposed to one it
# only mentions in a comment; the basenames on it are the candidates.
_PROVISIONER_CHAIN_LINE = re.compile(r"^\s*(?:bash|\.)\s+(.*)$", re.MULTILINE)

_SHELL_SCRIPT_BASENAME = re.compile(r"([\w.-]+\.sh)\b")


def read_provisioner_inputs(repo_root: Path) -> frozenset[str]:
    """The repo-relative files whose change means the provisioner must re-run.

    Its entry point, the apt snapshot timestamp, and every sibling installer
    the entry point chains -- read off the entry point in the tree at
    ``repo_root`` (the merged tree, so a release that adds or retires an
    installer is read as it ships) and kept only when the sibling exists there,
    which drops the image-baked and ``/tmp`` paths the script also invokes.
    """
    inputs = {PROVISIONER_SCRIPT, _APT_SNAPSHOT_TIMESTAMP}
    script = repo_root / PROVISIONER_SCRIPT
    if script.is_file():
        for line in _PROVISIONER_CHAIN_LINE.findall(script.read_text()):
            for name in _SHELL_SCRIPT_BASENAME.findall(line):
                if (script.parent / name).is_file():
                    inputs.add(f"{script.parent.relative_to(repo_root)}/{name}")
    return frozenset(inputs)


def _is_provisioner(path: str) -> bool:
    """Whether ``path`` shapes how the workspace/agent is *provisioned*.

    The provisioner's entry point plus everything under ``.mngr/`` -- the
    ``mngr create`` defaults, provider blocks, and the agent Claude-version pin
    that provisioning applies to every new workspace. The installers the entry
    point chains classify as ``shared_runtime`` like their neighbours; the
    apply re-runs the provisioner for them all the same.
    """
    return path == PROVISIONER_SCRIPT or path.startswith(".mngr/")


# Basenames whose change means a dependency manifest moved, so the editable
# install / build needs its env refreshed rather than just picking up new source.
_MANIFEST_BASENAMES = frozenset(
    {"pyproject.toml", "uv.lock", "package.json", "package-lock.json"}
)


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith("_test.py") or name.startswith("test_")


class PathClass(NamedTuple):
    """How one changed path should be applied and validated.

    ``reveal_class`` selects the go-live action -- named for the reveal step
    the atomic ``apply`` replaced, and kept because it is the wire name in
    ``classify-merge``'s JSON, which the skill and worker prose read;
    ``project`` is the pytest
    project whose suite covers the path (``.`` = the root workspace,
    ``system/apps/system_interface`` and ``system/apps/chat`` run their own suites);
    ``is_manifest`` flags a dependency-manifest change that needs an env refresh.
    """

    reveal_class: str
    project: str
    is_manifest: bool


def _project_for_path(path: str) -> str:
    """Return the pytest project root that owns ``path``.

    Only ``system/apps/system_interface`` and ``system/apps/chat`` carry their own
    pytest config (the root config ignores them); everything else -- libs, scripts,
    ``.agents`` -- is covered by the root suite, reported as ``.``.
    """
    if path.startswith("system/apps/system_interface/"):
        return "system/apps/system_interface"
    if path.startswith("system/apps/chat/"):
        return "system/apps/chat"
    return "."


def classify_path(path: str) -> PathClass:
    """Map a repo-relative path to its change class, test project, and manifest flag.

    The classes drive what ``apply`` does, and what the worker's report has to
    cover for the lead to finish the rest:

    - ``system_interface`` -- ``system/apps/system_interface/**``; the apply
      rebuilds or installs the bundle and re-syncs the workspace venv when the
      app's ``pyproject.toml`` changes (:func:`sync_workspace_venv`).
    - ``service`` -- ``system/supervisord.conf``, the per-program drop-ins under
      ``system/supervisord.conf.d/**``, and ``system/libs/bootstrap/**``.
    - ``shared_runtime`` -- ``system/scripts/**``, other ``system/libs/**``,
      ``system/services/**``, ``system/apps/**``, and ``.agents/**``: may be a live runtime dependency of
      a service or a workspace-added skill or app, so it needs the worker's
      impact analysis before it can be called a silent merge.
    - ``provisioner`` -- the pinned-toolchain entry point and the ``.mngr/``
      create config (see :func:`_is_provisioner`); shapes image-build /
      create-time provisioning, so a change is never applied by a service
      restart alone: the apply re-runs the idempotent provisioner for the
      files it reads (:func:`read_provisioner_inputs`), and the create config
      is the worker's to mirror live or flag for a workspace rebuild.
    - ``dockerfile`` -- ``system/Dockerfile``; split by hunk into live-applicable
      vs rebuild-only by worker judgement.
    - ``docs`` -- a ``README.md`` or a ``changelog/*.md`` entry wherever it lives,
      ``CLAUDE.md``, and any other ``*.md`` outside the prefixes above. A
      ``SKILL.md`` under ``.agents/`` is *not* docs: a skill's prose is what an
      agent runs, so it stays ``shared_runtime``.
    - ``other`` -- anything else.
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
        return PathClass(CLASS_DOCS, project, is_manifest)
    # Provisioning files are matched before the generic ``system/scripts/`` and
    # catch-all rules below: the toolchain entry point lives under
    # ``system/scripts/`` (would otherwise read as ``shared_runtime``) and
    # ``.mngr/settings.toml`` would otherwise fall through to ``other`` --
    # either way the worker would miss its build/create-time impact.
    if _is_provisioner(path):
        return PathClass(CLASS_PROVISIONER, project, is_manifest)
    if path.startswith("system/apps/system_interface/"):
        return PathClass(CLASS_SYSTEM_INTERFACE, project, is_manifest)
    if (
        path == "system/supervisord.conf"
        or path.startswith("system/supervisord.conf.d/")
        or path.startswith("system/libs/bootstrap/")
    ):
        return PathClass(CLASS_SERVICE, project, is_manifest)
    if path == "system/Dockerfile":
        return PathClass(CLASS_DOCKERFILE, project, is_manifest)
    if (
        path.startswith("system/scripts/")
        or path.startswith(".agents/")
        or path.startswith("system/libs/")
        or path.startswith("system/services/")
        or path.startswith("system/apps/")
    ):
        return PathClass(CLASS_SHARED_RUNTIME, project, is_manifest)
    if path == "CLAUDE.md" or "/changelog/" in path or path.endswith(".md"):
        return PathClass(CLASS_DOCS, project, is_manifest)
    return PathClass(CLASS_OTHER, project, is_manifest)


class MergeClassification(NamedTuple):
    """Every file either side changed, split by disposition, with per-file class info.

    ``merged`` are files where local also diverged (reconcile + validate);
    ``pulled_in`` are clean upstream arrivals local left untouched (trust, but
    still apply). Each entry is a dict with ``path``, ``reveal_class``,
    ``project``, ``is_manifest``, ``disposition``. The summary fields collect the
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

    ``local_only`` are the files only the local side changed, which are not
    upstream updates at all but are the workspace's own content; together with
    the merged set they are where a user-created consumer of the update can
    live. ``has_local_footprint`` is true when any of that local content is
    outside the docs class: a workspace whose only divergence is prose (the
    version history every apply rewrites, a ``PURPOSE.md``) has no code of its
    own for the update to break, so the impact analysis and the validation
    that exist to protect it have nothing to look at.
    """

    merged: list[dict[str, object]]
    pulled_in: list[dict[str, object]]
    reveal_classes_merged: list[str]
    reveal_classes_pulled_in: list[str]
    projects_to_validate: list[str]
    has_merge_work: bool
    local_only: list[dict[str, object]]
    has_local_footprint: bool


def _entry(path: str, disposition: str) -> dict[str, object]:
    info = classify_path(path)
    return {
        "path": path,
        "reveal_class": info.reveal_class,
        "project": info.project,
        "is_manifest": info.is_manifest,
        "disposition": disposition,
    }


def classify_merge(
    upstream_changed: Sequence[str], local_changed: Sequence[str]
) -> MergeClassification:
    """Split the changed files into the merged, pulled-in and local-only sets.

    ``upstream_changed`` is the set of files upstream changed relative to the
    merge base; ``local_changed`` the set the local branch changed relative to
    the same base. A file in both diverged on both sides -> **merged** (validate);
    a file only upstream changed is a clean **pulled-in** arrival (trust). Files
    only *local* changed are not upstream updates at all: they are reported as
    **local_only**, the workspace's own content, which is where a consumer of
    the update can live.
    """
    upstream = set(upstream_changed)
    local = set(local_changed)
    merged: list[dict[str, object]] = []
    pulled_in: list[dict[str, object]] = []
    for path in sorted(upstream):
        if path in local:
            merged.append(_entry(path, "merged"))
        else:
            pulled_in.append(_entry(path, "pulled_in"))
    local_only = [_entry(path, "local_only") for path in sorted(local - upstream)]

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
        local_only=local_only,
        has_local_footprint=any(
            entry["reveal_class"] != CLASS_DOCS for entry in merged + local_only
        ),
    )


def _is_backend_manifest(path: str) -> bool:
    """Whether ``path`` can change what both shared backend environments resolve to:
    the mngr tool and the workspace venv.

    The root ``pyproject.toml`` pins the mngr commit both install from, so a move of
    that pin (and the ``uv.lock`` that follows it) moves the chat app's and the
    shell's dependency closure as well as the tool's.

    The plugin manifest counts without being a Python manifest: it is what
    :func:`_manifest_extras` unions into the mngr tool's reinstall, so it decides
    which plugins that tool carries. Without it a release that only assigns an
    existing plugin to the tool would never trigger the refresh that registers it,
    and the updated workspace would diverge from a freshly built image -- which is
    the state the manifest exists to prevent.
    """
    return path in ("uv.lock", "pyproject.toml", PLUGIN_MANIFEST_PATH)


def _is_app_pyproject(path: str) -> bool:
    """Whether ``path`` is an app's ``pyproject.toml``, which only the workspace venv
    installs.

    The venv is the only install of each app, and a change to an app's console
    scripts reaches it only through a sync, even when the lockfile does not move. The
    mngr tool reads nothing an app declares, so it is left alone.
    """
    parts = path.split("/")
    return (
        len(parts) == 4
        and f"{parts[0]}/{parts[1]}" == APPS_DIR
        and parts[3] == "pyproject.toml"
    )


class ApplyPlan(NamedTuple):
    """What one apply must do beyond the restart, derived from the merged diff.

    Every apply restarts the services agent, pre-flights the merged backend
    first, and probes afterwards; the plan only decides the work in front of
    that. The frontend split (source vs npm manifest) and ``backend_src`` are
    finer than :func:`classify_path`'s single ``system_interface`` class because
    they need different work. ``backend_manifest`` is a root manifest that moves
    both shared environments (the mngr tool's reinstall and the venv's sync);
    ``app_pyproject`` is any app's ``pyproject.toml``, the shell's included,
    which only the venv installs; ``resyncs_venv`` is either. ``provisioner`` is
    the pinned-toolchain re-run, keyed on the files the provisioner reads.
    """

    frontend_src: bool
    frontend_manifest: bool
    backend_src: bool
    backend_manifest: bool
    app_pyproject: bool
    provisioner: bool

    @property
    def frontend(self) -> bool:
        return self.frontend_src or self.frontend_manifest

    @property
    def backend(self) -> bool:
        return self.backend_src or self.backend_manifest

    @property
    def resyncs_venv(self) -> bool:
        return self.backend_manifest or self.app_pyproject


def plan_apply(
    paths: Sequence[str],
    provisioner_inputs: Collection[str],
) -> ApplyPlan:
    """Classify the merged diff's ``paths`` into an :class:`ApplyPlan`.

    ``provisioner_inputs`` is :func:`read_provisioner_inputs` for the tree being
    applied. The frontend build
    output (``static/``) and ``node_modules`` are gitignored and never appear
    in a diff; they are covered by snapshots, not the plan.
    """
    frontend_src = False
    frontend_manifest = False
    backend_src = False
    backend_manifest = False
    app_pyproject = False
    provisioner = False
    for path in paths:
        if path in provisioner_inputs:
            provisioner = True
        if path == PYPROJECT_PATH:
            # The mngr pin here decides which commit ``system/vendor/mngr-assets`` is
            # fetched from, and every bundle compiles its embed contract and service
            # icons in, so a pin move re-emits them like a frontend source change.
            frontend_src = True
        if path in NPM_MANIFEST_PATHS:
            frontend_manifest = True
        elif path in FRONTEND_TOOLING_PATHS or any(
            path.startswith(f"{directory}/") for directory in FRONTEND_SOURCE_DIRS
        ):
            # Everything under a frontend counts, not just src/: the entry
            # document, the vite and TypeScript configs and the public assets
            # all change the emitted bundle; so does the shared library, which
            # every bundle compiles in, and the tooling every build reads.
            frontend_src = True
        elif _is_backend_manifest(path):
            backend_manifest = True
        elif _is_app_pyproject(path):
            app_pyproject = True
        elif (
            path.startswith(f"{SYSTEM_INTERFACE_DIR}/imbue/")
            and path.endswith(".py")
            and not _is_test_file(path)
        ):
            backend_src = True
    return ApplyPlan(
        frontend_src=frontend_src,
        frontend_manifest=frontend_manifest,
        backend_src=backend_src,
        backend_manifest=backend_manifest,
        app_pyproject=app_pyproject,
        provisioner=provisioner,
    )
