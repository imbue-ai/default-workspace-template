#!/usr/bin/env bash
#
# pull_upstreams.sh -- the reverse of push_upstreams.sh: pull this workspace's two
# GitHub upstreams back down.
#
#   1. system/vendor/mngr  <-  imbue-ai/mngr-internal            <mngr_branch>
#         re-vendor: updates the vendored copy IN your working tree (adds/edits only).
#   2. dwt branch          <-  imbue-ai/default-workspace-template <dwt_branch>
#         fetch + report only. That branch is a PROJECTION of your work (push_upstreams
#         strips the vendored mngr and writes a synthetic commit), so it has no clean
#         merge back into your working branch -- we fetch it and show what's new, and
#         leave integrating it to you.
#
# Usage:
#   system/scripts/pull_upstreams.sh <dwt_branch> <mngr_branch> [--dry-run] [--commit] [--yes]
#   system/scripts/pull_upstreams.sh <dwt_branch> --dwt-only [--dry-run]
#
# Options:
#   --dry-run   Show what WOULD change; touch nothing.
#   --commit    After re-vendoring mngr, make the re-vendor commit (else leaves it unstaged).
#   --yes       Skip the confirm before that commit.
#   --dwt-only  Pull ONLY from dwt (fetch + report). Skips the mngr re-vendor entirely,
#               so system/vendor/mngr is never touched. Needs only <dwt_branch>
#               (any <mngr_branch> is ignored).
#
# The mngr re-vendor makes the vendored subtrees MATCH the mngr branch:
#   * UPDATES every file already vendored under system/vendor/mngr.
#   * ADDS a new upstream file when its directory is already vendored (it belongs to a
#     subtree we track). A file outside every vendored directory -- .minds/template/, dev/,
#     a new top-level tree -- is mngr-repo infrastructure and is only REPORTED.
#   * DELETES files vendored here but gone upstream, via `git rm` so they land in the diff.
#     A vendored copy of mngr must not hold a file mngr itself deleted.
#
# SAFETY:
#   * URL-ONLY: never runs `git remote add`; URLs live only in a throwaway clone/fetch.
#   * Refuses if system/vendor/mngr already has uncommitted changes (won't clobber), so the
#     adds/deletes are always reviewable against a clean base.
#   * --dry-run reports every add/update/delete and touches nothing.
#   * Only your working tree under system/vendor/mngr is written; the dwt side is read-only.
#
set -euo pipefail

readonly MNGR_URL="https://github.com/imbue-ai/mngr-internal.git"
readonly DWT_URL="https://github.com/imbue-ai/default-workspace-template.git"
readonly PREFIX="system/vendor/mngr"

DWT_BRANCH=""
MNGR_BRANCH=""
DRY_RUN=0
DO_COMMIT=0
ASSUME_YES=0
DWT_ONLY=0

# Print the header comment block (lines 2 up to the `set -euo` line) as usage.
usage() { sed -n '2,/^set /{/^set /!p;}' "$0"; exit "${1:-1}"; }

positional=()
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run|-n) DRY_RUN=1; shift ;;
        --commit) DO_COMMIT=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
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

# rsync is only needed for the mngr re-vendor leg, which --dwt-only skips.
[ "$DWT_ONLY" -eq 1 ] || command -v rsync >/dev/null || { echo "error: rsync is required" >&2; exit 1; }

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# The re-vendor writes under $PREFIX, so it must be clean first. --dwt-only never
# writes there, so the check does not apply.
if [ "$DWT_ONLY" -eq 0 ] && { ! git diff --quiet -- "$PREFIX" || ! git diff --cached --quiet -- "$PREFIX"; }; then
    echo "REFUSING: $PREFIX has uncommitted changes -- commit or stash them first." >&2
    exit 2
fi

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

echo "=============================================================="
echo " pull plan"
if [ "$DWT_ONLY" -eq 1 ]; then
    echo "   dwt   dwt ${DWT_BRANCH}  ->  (fetch + report only; you integrate)"
    echo "   MODE: --dwt-only (mngr re-vendor skipped; ${PREFIX} untouched)"
else
    echo "   mngr  mngr-internal ${MNGR_BRANCH}  ->  ${PREFIX}  (re-vendor; updates working tree)"
    echo "   dwt   dwt ${DWT_BRANCH}              ->  (fetch + report only; you integrate)"
fi
[ "$DRY_RUN" -eq 1 ] && echo "   MODE: --dry-run (show changes, touch nothing)"
echo "=============================================================="

# ============================================================================
# 1) mngr re-vendor: mngr-internal <mngr_branch> -> system/vendor/mngr
#    Skipped entirely in --dwt-only.
# ============================================================================
if [ "$DWT_ONLY" -eq 0 ]; then
echo
echo ">>> [1/2] re-vendor mngr from ${MNGR_BRANCH}"
echo "    cloning ${MNGR_BRANCH} from mngr-internal (shallow; progress below)..."
if ! git clone --depth 1 --single-branch --branch "$MNGR_BRANCH" "$MNGR_URL" "$TMP/repo"; then
    echo "    error: could not clone '$MNGR_BRANCH' from ${MNGR_URL} (branch name? gh auth?)." >&2
    exit 1
fi
UPSTREAM_SHA="$(git -C "$TMP/repo" rev-parse HEAD)"

# mngr-internal is private and this repo is public, so only mngr's public subset may be
# vendored. Materialize it from the clone and treat THAT as upstream from here on: a raw
# rsync would re-add excluded files landing in an already-vendored directory, and would
# restore the unstripped content of files carrying internal blocks.
echo "    materializing mngr's public subset..."
if ! python3 "$TMP/repo/scripts/public_subset.py" "$TMP/subset" --repo-root "$TMP/repo" --ref HEAD --quiet; then
    echo "    error: could not materialize the public subset of ${MNGR_BRANCH}." >&2
    exit 1
fi

git ls-files "$PREFIX" | sed "s|^$PREFIX/||" | sort > "$TMP/vendored.txt"
( cd "$TMP/subset" && find . \( -type f -o -type l \) | sed 's|^\./||' ) | sort > "$TMP/upstream.txt"
comm -23 "$TMP/upstream.txt" "$TMP/vendored.txt" > "$TMP/upstream_only.txt"   # new upstream
comm -13 "$TMP/upstream.txt" "$TMP/vendored.txt" > "$TMP/vendored_only.txt"   # gone upstream -> deleted

# Split the new-upstream files by whether their directory is ALREADY vendored. A file landing in
# a directory we vendor belongs to a subtree we track, so it is added; one landing anywhere else
# (`.minds/template/`, `dev/`, a new top-level tree) is mngr-repo infrastructure with no meaning
# in a workspace, so it is only reported. This keeps "which subtrees do we vendor" implicit in
# the tree itself rather than in a hand-maintained list that drifts.
sed 's|/[^/]*$||' "$TMP/vendored.txt" | sort -u > "$TMP/vendored_dirs.txt"
: > "$TMP/to_add.txt"; : > "$TMP/skipped_add.txt"
while IFS= read -r rel; do
    dir="${rel%/*}"
    [ "$dir" = "$rel" ] && dir="."
    if grep -qxF -- "$dir" "$TMP/vendored_dirs.txt"; then
        printf '%s\n' "$rel" >> "$TMP/to_add.txt"
    else
        printf '%s\n' "$rel" >> "$TMP/skipped_add.txt"
    fi
done < "$TMP/upstream_only.txt"

# rsync --existing: update files that ALREADY exist under $PREFIX; never create, never delete.
# --checksum compares by CONTENT; we drop time-sync (-rlpgoD = -a minus -t) so a fresh clone's
# all-new mtimes don't flag identical files. git ignores mtime, so this changes nothing real.
RSYNC_FLAGS=(-rlpgoD --checksum --existing --exclude='.git/')
if [ "$DRY_RUN" -eq 1 ]; then
    rsync -in "${RSYNC_FLAGS[@]}" "$TMP/subset/" "$PREFIX/" > "$TMP/changes.txt" || true
    if [ -s "$TMP/changes.txt" ]; then
        echo "    would update:"; sed 's/^/      /' "$TMP/changes.txt"
    else
        echo "    (no vendored files differ from ${MNGR_BRANCH})"
    fi
else
    rsync "${RSYNC_FLAGS[@]}" "$TMP/subset/" "$PREFIX/"
    git --no-pager diff --stat -- "$PREFIX" > "$TMP/diffstat.txt" || true
    if [ -s "$TMP/diffstat.txt" ]; then
        echo "    updated (in your working tree, unstaged):"; sed 's/^/      /' "$TMP/diffstat.txt"
    else
        echo "    (no vendored files differ from ${MNGR_BRANCH})"
    fi
fi

if [ -s "$TMP/to_add.txt" ]; then
    echo
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "    would add -- $(wc -l < "$TMP/to_add.txt") new upstream file(s) in already-vendored directories:"
    else
        while IFS= read -r rel; do
            mkdir -p "$PREFIX/${rel%/*}"
            cp -p "$TMP/subset/$rel" "$PREFIX/$rel"
        done < "$TMP/to_add.txt"
        git add -- "$PREFIX" >/dev/null 2>&1 || true
        echo "    added -- $(wc -l < "$TMP/to_add.txt") new upstream file(s) in already-vendored directories:"
    fi
    sed 's/^/      + /' "$TMP/to_add.txt" | head -n 40
    [ "$(wc -l < "$TMP/to_add.txt")" -gt 40 ] && echo "      ... (truncated)"
fi
if [ -s "$TMP/skipped_add.txt" ]; then
    echo
    echo "    NOT added -- $(wc -l < "$TMP/skipped_add.txt") upstream file(s) outside every vendored directory:"
    sed 's/^/      ~ /' "$TMP/skipped_add.txt" | head -n 40
    [ "$(wc -l < "$TMP/skipped_add.txt")" -gt 40 ] && echo "      ... (truncated)"
fi
if [ -s "$TMP/vendored_only.txt" ]; then
    echo
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "    would delete -- $(wc -l < "$TMP/vendored_only.txt") file(s) vendored here but gone upstream:"
    else
        # git rm so the removals land in the diff and are reviewable. A vendored copy of mngr has
        # no business holding a file mngr itself deleted.
        ( cd "$PREFIX" && sed 's|^|./|' "$TMP/vendored_only.txt" | tr '\n' '\0' \
            | xargs -0 --no-run-if-empty git rm -q --ignore-unmatch -- )
        echo "    deleted -- $(wc -l < "$TMP/vendored_only.txt") file(s) vendored here but gone upstream:"
    fi
    sed 's/^/      - /' "$TMP/vendored_only.txt" | head -n 40
    [ "$(wc -l < "$TMP/vendored_only.txt")" -gt 40 ] && echo "      ... (truncated)"
fi

if [ "$DRY_RUN" -eq 0 ] && [ "$DO_COMMIT" -eq 1 ]; then
    if git diff --quiet -- "$PREFIX"; then
        echo; echo "    nothing to commit."
    else
        do_it=1
        if [ "$ASSUME_YES" -eq 0 ]; then
            printf '\n    commit the re-vendor to your current branch? [type "yes"]: ' >&2
            read -r a </dev/tty || a=""; [ "$a" = "yes" ] || do_it=0
        fi
        if [ "$do_it" -eq 1 ]; then
            git add -- "$PREFIX"
            git commit -qm "$PREFIX: re-vendor from mngr-internal ${MNGR_BRANCH} @ ${UPSTREAM_SHA:0:12}"
            echo "    committed re-vendor ($(git rev-parse --short HEAD))."
        else
            echo "    left changes unstaged."
        fi
    fi
fi
fi   # end: mngr re-vendor leg (skipped in --dwt-only)

# ============================================================================
# 2) dwt: fetch + report (read-only). No auto-merge -- it is a projection.
# ============================================================================
echo
if [ "$DWT_ONLY" -eq 1 ]; then
    echo ">>> [1/1] dwt ${DWT_BRANCH} (fetch + report)"
else
    echo ">>> [2/2] dwt ${DWT_BRANCH} (fetch + report)"
fi
if ! git fetch --quiet "$DWT_URL" "$DWT_BRANCH" 2>/dev/null; then
    echo "    warning: could not fetch '$DWT_BRANCH' from ${DWT_URL} (skipping)." >&2
else
    DWT_TIP="$(git rev-parse FETCH_HEAD)"
    git --no-pager log --oneline HEAD..FETCH_HEAD > "$TMP/incoming.txt" || true
    if [ -s "$TMP/incoming.txt" ]; then
        echo "    ${DWT_BRANCH} has $(wc -l < "$TMP/incoming.txt") commit(s) not in your current branch:"
        sed 's/^/      /' "$TMP/incoming.txt" | head -n 20
        echo "    to integrate (review first -- this branch is a projection of your work):"
        echo "      git merge ${DWT_TIP}"
    else
        echo "    up to date (nothing on ${DWT_BRANCH} that isn't already in your branch)."
    fi
fi

echo
echo "done.$([ "$DRY_RUN" -eq 1 ] && echo ' (dry-run: nothing written)')"
