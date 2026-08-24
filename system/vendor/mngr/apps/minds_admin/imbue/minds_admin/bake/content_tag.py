"""Content-addressed image-cache tags for CI slice bakes.

The per-box image cache needs an immutable key. Production ``--from-tag`` bakes
key on the tag; ``--workspace-dir`` bakes have historically disabled the cache
because a branch label is mutable content. CI bakes (unpinned content that
changes every run) instead key on a hash of the workspace content itself: the
git tree hash of the workspace dir computed through a temporary index, which
covers tracked modifications and untracked files (including the freshly-synced
``system/vendor/mngr``), respects .gitignore exactly like the vendor rsync, and
is byte-stable across runs with identical content (tree hashes carry no
timestamps).

The docker build is not a pure function of the tree (base-image pulls, apt), so
a cache hit can serve a slightly staler build than a from-scratch one would --
the same tradeoff production tag caching already accepts.
"""

import os
import tempfile
from pathlib import Path
from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_imbue_cloud.errors import ImbueCloudError

# Docker image repository every cache tag lives under -- content-addressed tags
# here and ``allocate_slices``'s release-tag cache alike -- so both kinds of tar
# share one box cache dir (and its publish-time eviction).
DEFAULT_WORKSPACE_TEMPLATE_IMAGE_REPOSITORY: Final[str] = "default-workspace-template"
# Distinguishes content-derived tags from real release tags at a glance.
_CONTENT_TAG_PREFIX: Final[str] = "content-"
_GIT_TIMEOUT_SECONDS: Final[float] = 300.0


class ContentTagError(ImbueCloudError, ValueError):
    """Raised when a content-addressed cache tag cannot be derived from a workspace dir."""


def _run_git(workspace_dir: Path, args: list[str], *, index_file: Path, cg_name: str) -> str:
    # The throwaway index redirects staging away from the repo's real index; the
    # rest of the environment is inherited so git itself resolves normally.
    subprocess_env = dict(os.environ)
    subprocess_env["GIT_INDEX_FILE"] = str(index_file)
    cg = ConcurrencyGroup(name=cg_name)
    with cg:
        result = cg.run_process_to_completion(
            command=["git", "-C", str(workspace_dir), *args],
            timeout=_GIT_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=subprocess_env,
        )
    if result.returncode != 0:
        raise ContentTagError(
            f"`git {' '.join(args)}` in {workspace_dir} exited {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def compute_content_addressed_cache_tag(workspace_dir: Path) -> str:
    """Derive the image-cache tag for a workspace dir from its content.

    Stages the working tree (tracked changes + untracked non-ignored files)
    into a throwaway index and hashes it with ``git write-tree``, so the tag is
    a pure function of the content the bake will actually ship. Raises
    :class:`ContentTagError` when ``workspace_dir`` is not a git work tree.
    """
    if not (workspace_dir / ".git").exists():
        raise ContentTagError(f"cannot derive a content-addressed cache tag: {workspace_dir} is not a git work tree")
    with tempfile.TemporaryDirectory(prefix="mngr-content-tag-") as temp_dir:
        index_file = Path(temp_dir) / "index"
        _run_git(workspace_dir, ["add", "-A"], index_file=index_file, cg_name="content-tag-add")
        tree_hash = _run_git(workspace_dir, ["write-tree"], index_file=index_file, cg_name="content-tag-write-tree")
    if not tree_hash:
        raise ContentTagError(f"`git write-tree` in {workspace_dir} produced no tree hash")
    return f"{DEFAULT_WORKSPACE_TEMPLATE_IMAGE_REPOSITORY}:{_CONTENT_TAG_PREFIX}{tree_hash}"
