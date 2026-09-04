#!/usr/bin/env bash
#
# push_upstreams.sh -- split this workspace's committed work to its two
# upstreams, keeping the vendored mngr changes OUT of the dwt branch.
#
#   1. system/vendor/mngr  ->  imbue-ai/mngr-internal            <mngr_branch>
#   2. the workspace       ->  imbue-ai/default-workspace-template <dwt_branch>
#      (with system/vendor/mngr reset to whatever the dwt branch already pins,
#       so no mngr diff ever lands on dwt)
#
# Usage:
#   system/scripts/push_upstreams.sh <dwt_branch> <mngr_branch> [options]
#   system/scripts/push_upstreams.sh <dwt_branch> --dwt-only [options]
#
# Options:
#   --from <ref>   Commit to split (default: HEAD). Only committed work is pushed.
#   --dry-run      Build both commits in throwaway checkouts and show their diffs,
#                  but do NOT push anything. Nothing leaves your machine.
#   --yes          Skip the interactive confirm before each push.
#   --dwt-only     Push ONLY the workspace to dwt, with system/vendor/mngr included
#                  verbatim (no vendoring split). The mngr-internal push is skipped
#                  and the dwt commit carries the FULL tree. Needs only <dwt_branch>
#                  (any <mngr_branch> is ignored).
#
# SAFETY (why this is agent-proof):
#   * URL-ONLY. It never runs `git remote add`; the upstream URLs live only inside
#     throwaway temp checkouts that are deleted at the end. No new remote is ever
#     attached to your repo, so nothing can be `git push`ed there by accident later.
#   * NEVER main. Refuses if either target branch is main/master, and asserts the
#     built dwt commit contains zero system/vendor/mngr paths before pushing.
#   * NO HOOKS. Every internal commit runs with an empty core.hooksPath, so a
#     post-commit / github-sync auto-push cannot fire on a temp branch.
#   * READ-ONLY on your checkout. All work happens in temp worktrees/clones; your
#     current branch, index, and working tree are never touched.
#
set -euo pipefail

# --- upstream URLs (used directly, never added as remotes) --------------------
readonly MNGR_URL="https://github.com/imbue-ai/mngr-internal.git"
readonly DWT_URL="https://github.com/imbue-ai/default-workspace-template.git"
readonly SUBTREE_PREFIX="system/vendor/mngr"

# --- args ---------------------------------------------------------------------
DWT_BRANCH=""
MNGR_BRANCH=""
SOURCE_REF="HEAD"
ASSUME_YES=0
DRY_RUN=0
DWT_ONLY=0

# Print the header comment block (lines 2 up to the `set -euo` line) as usage.
usage() { sed -n '2,/^set /{/^set /!p;}' "$0"; exit "${1:-1}"; }

positional=()
while [ $# -gt 0 ]; do
    case "$1" in
        --from) SOURCE_REF="${2:?--from needs a ref}"; shift 2 ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --dry-run|-n) DRY_RUN=1; shift ;;
        --dwt-only) DWT_ONLY=1; shift ;;
        -h|--help) usage 0 ;;
        -*) echo "unknown option: $1" >&2; usage 1 ;;
        *) positional+=("$1"); shift ;;
    esac
done
if [ "$DWT_ONLY" -eq 1 ]; then
    [ "${#positional[@]}" -ge 1 ] || { echo "error: --dwt-only needs <dwt_branch>" >&2; usage 1; }
    DWT_BRANCH="${positional[0]}"
    MNGR_BRANCH=""   # unused in --dwt-only
else
    [ "${#positional[@]}" -eq 2 ] || { echo "error: need <dwt_branch> and <mngr_branch>" >&2; usage 1; }
    DWT_BRANCH="${positional[0]}"
    MNGR_BRANCH="${positional[1]}"
fi

# mngr is no longer vendored: it is installed from the public repo at the commit
# pyproject.toml pins ([tool.uv.sources] imbue-mngr). Its dwt-side leg has nothing
# to push; change mngr in a checkout of imbue-ai/mngr-internal and, once merged,
# move the pin here (`uv lock --upgrade-package imbue-mngr` after editing the rev).
if [ "$DWT_ONLY" -eq 0 ]; then
    echo "error: the mngr leg is gone -- mngr is a pinned dependency now (pyproject.toml, [tool.uv.sources])." >&2
    echo "       Use --dwt-only for the dwt leg; change mngr in an imbue-ai/mngr-internal checkout instead." >&2
    exit 2
fi

# --- guards -------------------------------------------------------------------
guarded_branches=("$DWT_BRANCH")
[ "$DWT_ONLY" -eq 0 ] && guarded_branches+=("$MNGR_BRANCH")
for b in "${guarded_branches[@]}"; do
    case "$b" in
        main|master|HEAD|"")
            echo "REFUSING: '$b' is not an allowed target branch (never main/master)." >&2
            exit 2 ;;
    esac
done

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
git rev-parse --verify --quiet "$SOURCE_REF^{commit}" >/dev/null \
    || { echo "error: --from ref '$SOURCE_REF' is not a commit" >&2; exit 1; }
# Pin to an absolute sha NOW: inside the dwt worktree "HEAD" would mean the detached
# dwt tip, not the commit we are splitting. Every read of the source uses this sha.
SOURCE_SHA="$(git rev-parse --verify "$SOURCE_REF^{commit}")"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "note: you have uncommitted changes; only committed work in '$SOURCE_REF' is pushed." >&2
fi

# Empty hooks dir: internal commits fire NO hooks (no accidental auto-push).
EMPTY_HOOKS="$(mktemp -d)"
NOHOOKS=(-c "core.hooksPath=$EMPTY_HOOKS")

# Cleanup of every temp artifact, always.
TMP_DIRS=()
DWT_WORKTREE=""
cleanup() {
    [ -n "$DWT_WORKTREE" ] && git worktree remove --force "$DWT_WORKTREE" 2>/dev/null || true
    for d in "${TMP_DIRS[@]:-}" "$EMPTY_HOOKS"; do [ -n "$d" ] && rm -rf "$d"; done
}
trap cleanup EXIT

confirm() {
    # In dry-run we NEVER push: report intent and return non-zero so the caller skips.
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "    (dry-run) would push here -- skipping (nothing leaves your machine)." >&2
        return 1
    fi
    [ "$ASSUME_YES" -eq 1 ] && return 0
    local answer
    printf '%s [type "yes" to push]: ' "$1" >&2
    read -r answer </dev/tty || answer=""
    [ "$answer" = "yes" ]
}

echo "=============================================================="
echo " push plan (source = $(git rev-parse --short "$SOURCE_REF"))"
if [ "$DWT_ONLY" -eq 1 ]; then
    echo "   dwt   (FULL workspace, incl ${SUBTREE_PREFIX})  ->  ${DWT_URL##*/}  branch ${DWT_BRANCH}"
    echo "   MODE: --dwt-only (mngr-internal push skipped; no vendoring split)"
else
    echo "   mngr  ${SUBTREE_PREFIX}  ->  ${MNGR_URL##*/}  branch ${MNGR_BRANCH}"
    echo "   dwt   (workspace)         ->  ${DWT_URL##*/}  branch ${DWT_BRANCH}"
    echo "   dwt keeps its pinned ${SUBTREE_PREFIX} (mngr diffs excluded)"
fi
[ "$DRY_RUN" -eq 1 ] && echo "   MODE: --dry-run (build + show diffs, push nothing)"
echo "=============================================================="

# ============================================================================
# 1) mngr subtree -> mngr-internal <mngr_branch>
#    OVERLAY our tracked subtree onto the review branch as one commit. We never
#    clear the clone first, because the vendored copy is a SUBSET of the mngr repo
#    (it intentionally omits paths like .minds/template/ and some mirror/ files) --
#    clearing would delete those upstream. Overlay = adds + edits only, no deletes.
# ============================================================================
# Skipped entirely in --dwt-only: no push to mngr-internal.
if [ "$DWT_ONLY" -eq 0 ]; then
echo
echo ">>> [1/2] mngr subtree -> ${MNGR_BRANCH}"
MNGR_CLONE="$(mktemp -d)"; TMP_DIRS+=("$MNGR_CLONE")
echo "    cloning ${MNGR_BRANCH} from mngr-internal (shallow; the slow step -- progress below)..."
# --depth 1: shallow, so a big repo clones in seconds (GitHub accepts a push of one
# commit onto the shallow tip). NOT quiet and NOT 2>/dev/null: you must see clone
# progress and any credential prompt instead of a silent hang.
if ! git clone --depth 1 --single-branch --branch "$MNGR_BRANCH" "$MNGR_URL" "$MNGR_CLONE/repo"; then
    echo "error: could not clone branch '$MNGR_BRANCH' from ${MNGR_URL}." >&2
    echo "       check the branch name exists there, and that git can auth (gh auth login)." >&2
    exit 1
fi
# Overlay our vendored subtree's TRACKED tree (git archive = tracked-only, so no
# .venv / caches). No `git rm` first: files present upstream but not vendored here
# are left untouched. (A deletion made *within* the vendored subtree therefore does
# not propagate -- handle those by hand on the rare occasion they happen.)
git archive "$SOURCE_SHA:$SUBTREE_PREFIX" | tar -x -C "$MNGR_CLONE/repo"
(
    cd "$MNGR_CLONE/repo"
    git add -A
    if git diff --cached --quiet; then
        echo "    mngr: no changes vs ${MNGR_BRANCH}; nothing to push."
    else
        git "${NOHOOKS[@]}" commit -qm "Vendor sync from default-workspace-template (${DWT_BRANCH})"
        echo "    --- mngr diff to push (stat) ---"
        git show --stat --format="    %h %s" HEAD
        if confirm "    push mngr -> origin ${MNGR_BRANCH}?"; then
            git push origin "$MNGR_BRANCH"
            echo "    pushed mngr -> ${MNGR_BRANCH}"
        else
            echo "    skipped mngr push."
        fi
    fi
)
fi   # end: mngr leg (skipped in --dwt-only)

# ============================================================================
# 2) workspace -> dwt <dwt_branch>.
#    Default: overlay the source's NON-mngr tree onto the published dwt tip and
#    keep the dwt branch's own system/vendor/mngr untouched (vendoring split).
#    --dwt-only: overlay the FULL source tree, system/vendor/mngr included.
#    Either way: fast-forward, one commit.
# ============================================================================
echo
if [ "$DWT_ONLY" -eq 1 ]; then
    echo ">>> [1/1] workspace -> ${DWT_BRANCH} (FULL tree, mngr included)"
else
    echo ">>> [2/2] workspace -> ${DWT_BRANCH} (mngr excluded)"
fi
if ! git fetch --quiet "$DWT_URL" "$DWT_BRANCH" 2>/dev/null; then
    echo "error: branch '$DWT_BRANCH' not found on ${DWT_URL}." >&2
    exit 1
fi
DWT_TIP="$(git rev-parse FETCH_HEAD)"
DWT_WORKTREE="$(mktemp -d)/wt"
git worktree add --quiet --detach "$DWT_WORKTREE" "$DWT_TIP"
(
    cd "$DWT_WORKTREE"
    if [ "$DWT_ONLY" -eq 1 ]; then
        # Full tree, mngr included: make the whole worktree match the source ref.
        git rm -rq --ignore-unmatch -- . >/dev/null
        git checkout "$SOURCE_SHA" -- .
    else
        # Make everything EXCEPT the vendored mngr match the source ref exactly
        # (handles adds, edits, and deletes); leave system/vendor/mngr as dwt pins it.
        git rm -rq --ignore-unmatch -- . ":(exclude)$SUBTREE_PREFIX" >/dev/null
        git checkout "$SOURCE_SHA" -- . ":(exclude)$SUBTREE_PREFIX"
    fi
    git add -A
    if git diff --cached --quiet; then
        echo "    dwt: no workspace changes vs ${DWT_BRANCH}; nothing to push."
        exit 0
    fi
    if [ "$DWT_ONLY" -eq 1 ]; then
        git "${NOHOOKS[@]}" commit -qm "dwt: full workspace sync (system/vendor/mngr included; --dwt-only)"
        echo "    --- dwt diff to push (stat; FULL tree incl ${SUBTREE_PREFIX}) ---"
    else
        git "${NOHOOKS[@]}" commit -qm "dwt: sync workspace changes (vendored mngr pinned; mngr work on ${MNGR_BRANCH})"
        # HARD ASSERT: in the split mode the dwt commit must not carry a single mngr path.
        if git show --name-only --format= HEAD | grep -q "^${SUBTREE_PREFIX}/"; then
            echo "    ABORT: system/vendor/mngr paths leaked into the dwt commit -- not pushing." >&2
            exit 1
        fi
        echo "    --- dwt diff to push (stat; must show NO ${SUBTREE_PREFIX}) ---"
    fi
    git show --stat --format="    %h %s" HEAD
    if confirm "    push dwt -> ${DWT_BRANCH}?"; then
        git push "$DWT_URL" "HEAD:$DWT_BRANCH"
        echo "    pushed dwt -> ${DWT_BRANCH}"
    else
        echo "    skipped dwt push."
    fi
)

echo
echo "done.$([ "$DRY_RUN" -eq 1 ] && echo ' (dry-run: nothing was pushed)')"
