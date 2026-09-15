from pathlib import Path
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_mapreduce.archive import ARCHIVE_FILENAME
from imbue.mngr_mapreduce.archive import ARCHIVE_SUBDIR

# Name of the git bundle inside an agent's outputs archive.
BRANCH_BUNDLE_NAME: Final[str] = "branch.bundle"

# Bash that packages ``.test_output`` into the outputs archive. The agent runs
# this from the git repo root as the final step of its prompt, writing via a
# ``.tmp`` sibling so the orchestrator never reads a half-written archive.
PUBLISH_OUTPUTS_SNIPPET: Final[str] = f"""```bash
ARCHIVE_DIR="$MNGR_AGENT_STATE_DIR/{ARCHIVE_SUBDIR}"
mkdir -p "$ARCHIVE_DIR"

STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

cp -a .test_output "$STAGING/test_output"

# Bundled with the explicit branch name so the orchestrator can fetch ``$BRANCH:$BRANCH``.
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ -n "$(git rev-list --max-count=1 "$MNGR_GIT_BASE_BRANCH..$BRANCH" 2>/dev/null)" ]; then
    git bundle create "$STAGING/{BRANCH_BUNDLE_NAME}" "$MNGR_GIT_BASE_BRANCH..$BRANCH"
fi

TARBALL="$ARCHIVE_DIR/{ARCHIVE_FILENAME}"
tar -czf "$TARBALL.tmp" -C "$STAGING" .
mv "$TARBALL.tmp" "$TARBALL"
```"""


def apply_branch_bundle(
    source_dir: Path,
    bundle_path: Path,
    branch_name: str,
    agent_name: str,
    cg: ConcurrencyGroup,
) -> bool:
    """Fetch a branch from a bundle into the local source_dir repo.

    The bundle was created with ``git bundle create ... <base>..<branch>``,
    so it carries the ref under its branch name; the fetch refspec maps
    that ref onto the same local branch name. Idempotent for repeated
    invocations. Returns True on success.
    """
    result = cg.run_process_to_completion(
        ["git", "fetch", "--no-tags", str(bundle_path), f"+{branch_name}:{branch_name}"],
        cwd=source_dir,
        is_checked_after=False,
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to apply branch bundle for agent '{}' (branch {}): {}",
            agent_name,
            branch_name,
            result.stderr.strip(),
        )
        return False
    logger.debug("Applied branch bundle for agent '{}' onto branch '{}'", agent_name, branch_name)
    return True


def has_local_branch(source_dir: Path, branch_name: str, cg: ConcurrencyGroup) -> bool:
    result = cg.run_process_to_completion(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=source_dir,
        is_checked_after=False,
    )
    return result.returncode == 0
