"""How a changed path is applied and validated: the change classes ``classify-merge``
reports to the worker, the provisioner's inputs, and the apply plan derived from a
merged diff.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Collection, NamedTuple, Sequence

from update_layout import (
    FRONTEND_DIR,
    MNGR_VENDOR_DIR,
    PROVISIONER_SCRIPT,
    SYSTEM_INTERFACE_DIR,
)

CLASS_SYSTEM_INTERFACE = "system_interface"

CLASS_SERVICE = "service"

CLASS_EDITABLE_TOOL = "editable_tool"

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
    ``system/apps/system_interface`` and ``system/vendor/mngr`` run their own suites);
    ``is_manifest`` flags a dependency-manifest change that needs an env refresh.
    """

    reveal_class: str
    project: str
    is_manifest: bool


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
    - ``service`` -- ``system/supervisord.conf`` and ``system/libs/bootstrap/**``.
    - ``editable_tool`` -- ``system/vendor/mngr/**``; a manifest change needs
      ``uv sync --all-packages`` / an editable reinstall.
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
    if path == "system/supervisord.conf" or path.startswith("system/libs/bootstrap/"):
        return PathClass(CLASS_SERVICE, project, is_manifest)
    if path.startswith("system/vendor/mngr/"):
        return PathClass(CLASS_EDITABLE_TOOL, project, is_manifest)
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
    """The upstream-changed files split by disposition, with per-file class info.

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


def _is_backend_manifest(path: str) -> bool:
    """Whether ``path`` can change what the backend's environment resolves to.

    Not just the app's own manifest: the backend imports the vendored mngr and
    shells out to it, both as editable installs, so a vendored package's
    ``pyproject.toml`` moves their dependency closure exactly as the app's own
    does. Both workspace roots count; the vendored root is the one ``uv tool
    install -e system/vendor/mngr/libs/mngr`` resolves through.
    """
    if path in (
        f"{SYSTEM_INTERFACE_DIR}/pyproject.toml",
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


class ApplyPlan(NamedTuple):
    """What one apply must do beyond the restart, derived from the merged diff.

    Every apply restarts the services agent, pre-flights the merged backend
    first, and probes afterwards; the plan only decides the work in front of
    that. The system-interface split (frontend vs backend, source vs manifest)
    is finer than :func:`classify_path`'s single ``system_interface`` class
    because those four need different work; ``provisioner`` is the
    pinned-toolchain re-run, keyed on the files the provisioner reads.
    """

    frontend_src: bool
    frontend_manifest: bool
    backend_src: bool
    backend_manifest: bool
    provisioner: bool

    @property
    def frontend(self) -> bool:
        return self.frontend_src or self.frontend_manifest

    @property
    def backend(self) -> bool:
        return self.backend_src or self.backend_manifest


def plan_apply(paths: Sequence[str], provisioner_inputs: Collection[str]) -> ApplyPlan:
    """Classify the merged diff's ``paths`` into an :class:`ApplyPlan`.

    ``provisioner_inputs`` is :func:`read_provisioner_inputs` for the tree
    being applied. The frontend build output (``static/``) and
    ``node_modules`` are gitignored and never appear in a diff; they are
    covered by snapshots, not the plan.
    """
    frontend_src = False
    frontend_manifest = False
    backend_src = False
    backend_manifest = False
    provisioner = False
    for path in paths:
        if path in provisioner_inputs:
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
        provisioner=provisioner,
    )
