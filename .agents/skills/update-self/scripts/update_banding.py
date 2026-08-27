"""OOM banding for the apply orchestrator, loaded off the *pre-merge* tree's
``oom_priority`` package so a staged copy of this flow still runs on older trees.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Sequence

# The ``oom_priority`` bands module, when the tree carries it. Loaded lazily
# from the target repo root (this script may run as a staged copy far from any
# in-tree package) and guarded, so the staged copy still runs on trees that
# predate the package. ``None`` means no banding and no expendable tagging.
_BANDS = None

# Everything this script reads off that module -- which belongs to the
# *pre-merge* tree, so its surface is some older release's and an attribute
# added since is simply absent. Checked once at load, and a module missing any
# of it is refused wholesale, because the alternative is an ``AttributeError``
# mid-apply, past the merge and the snapshots, where nothing catches it and the
# workspace is left half-applied. A new read belongs in this tuple.
# ``UPDATE_APPLY`` is deliberately absent: it has a real fallback.
_REQUIRED_BANDS_ATTRIBUTES = (
    "AGENT_SUBPROCESS",
    "SERVICE_BANDS",
    "oom_tag_shell_prefix",
    "set_oom_score_adj",
)


def _load_bands(repo_root: Path):
    """Import ``oom_priority.bands`` from ``repo_root``'s tree, or ``None``.

    Deliberately not a module-level import: the apply is staged and executed
    from ``data/.tasks/update-self/skill-at-target/...``, so the package can
    only be found relative to the repo being applied to -- and an older tree
    may not carry it at all, or may carry a version of it predating part of
    :data:`_REQUIRED_BANDS_ATTRIBUTES`. Either must degrade to "no banding"
    rather than a crash (the staged copy runs against older pre-merge trees by
    design).
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
    missing = [name for name in _REQUIRED_BANDS_ATTRIBUTES if not hasattr(bands, name)]
    if missing:
        sys.stderr.write(
            f"note: {bands_src} carries an oom_priority package predating "
            f"{', '.join(missing)}, so this apply runs unbanded and its build steps "
            "are not tagged expendable; a memory shed during it may take the update "
            "rather than a rebuildable child.\n"
        )
        return None
    return bands


def protect_from_memory_shed(repo_root: Path) -> None:
    """Band this process into the near-exempt update-apply band.

    The apply orchestrator must outlive every agent, chat, and ordinary
    service: losing a build is an ordinary failure the rollback absorbs, but
    losing the apply mid-motion is the half-applied state this design exists to
    prevent. Only the authority paths that would repair a failed apply
    (owner-exec, the terminal) stay below it. The write succeeds from any
    launcher in the workspace -- a chat agent, the recovery cron, a terminal:
    the kernel only refuses an ``oom_score_adj`` below the lowest value the
    process has ever held (inherited across fork), and nothing in the
    container has ``CAP_SYS_RESOURCE``, the one capability that raises that
    floor, so it stays at 0 for every process tree (measured on the docker,
    lima and imbue_cloud providers). Best-effort all the same: an apply that
    cannot be protected is still an apply worth running. Called from
    ``__main__`` rather than from the command functions so exercising them in
    a test cannot re-band the test runner.
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
            "warning: could not lower this process's memory-shed priority; the apply "
            "keeps the band it was launched in, and a shed during it would skip the "
            "rollback.\n"
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

    A no-op passthrough when the pre-merge tree carries no usable
    ``oom_priority`` package (see :func:`_load_bands`).
    """
    if _BANDS is None:
        return list(argv)
    prefix = _BANDS.oom_tag_shell_prefix(_BANDS.AGENT_SUBPROCESS) + 'exec "$@"'
    return ["sh", "-c", prefix, "sh", *argv]
