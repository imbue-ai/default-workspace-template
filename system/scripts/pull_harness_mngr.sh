#!/usr/bin/env bash
#
# pull_harness_mngr.sh -- re-vendor system/vendor/mngr FROM mngr-internal <mngr_branch>.
# The reverse of the mngr half of push_harness_branches.sh: after your mngr changes are
# reviewed/merged on mngr-internal, this pulls them back into the vendored copy.
#
# Usage:
#   system/scripts/pull_harness_mngr.sh <mngr_branch> [--dry-run] [--commit] [--yes]
#
# Options:
#   --dry-run   Show what WOULD change in system/vendor/mngr; touch nothing.
#   --commit    After updating, make a re-vendor commit (otherwise leaves the changes
#               unstaged for you to review and commit yourself).
#   --yes       Skip the confirm before committing (only relevant with --commit).
#
# WHAT IT DOES (and deliberately does NOT do):
#   * UPDATES only files that already exist in system/vendor/mngr (the vendored SUBSET).
#   * NEVER adds mngr-repo-only paths (e.g. .minds/template/) -- those are intentionally
#     not vendored. New upstream files are REPORTED, not applied.
#   * NEVER deletes. Files vendored here but gone upstream are REPORTED, not removed.
#
# SAFETY:
#   * URL-ONLY: never runs `git remote add`; the URL lives only in a throwaway clone.
#   * Refuses if system/vendor/mngr already has uncommitted changes (won't clobber).
#   * Only your working tree under system/vendor/mngr is touched; nothing is pushed.
#
set -euo pipefail

readonly MNGR_URL="https://github.com/imbue-ai/mngr-internal.git"
readonly PREFIX="system/vendor/mngr"

MNGR_BRANCH=""
DRY_RUN=0
DO_COMMIT=0
ASSUME_YES=0

usage() { sed -n '2,28p' "$0"; exit "${1:-1}"; }

positional=()
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run|-n) DRY_RUN=1; shift ;;
        --commit) DO_COMMIT=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) usage 0 ;;
        -*) echo "unknown option: $1" >&2; usage 1 ;;
        *) positional+=("$1"); shift ;;
    esac
done
[ "${#positional[@]}" -eq 1 ] || { echo "error: need <mngr_branch>" >&2; usage 1; }
MNGR_BRANCH="${positional[0]}"

command -v rsync >/dev/null || { echo "error: rsync is required" >&2; exit 1; }

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Refuse to clobber in-progress vendored edits.
if ! git diff --quiet -- "$PREFIX" || ! git diff --cached --quiet -- "$PREFIX"; then
    echo "REFUSING: $PREFIX has uncommitted changes -- commit or stash them first." >&2
    exit 2
fi

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

echo "=============================================================="
echo " pull plan"
echo "   mngr-internal ${MNGR_BRANCH}  ->  ${PREFIX}  (update vendored files only)"
[ "$DRY_RUN" -eq 1 ] && echo "   MODE: --dry-run (show changes, touch nothing)"
echo "=============================================================="

echo "cloning ${MNGR_BRANCH} from mngr-internal (shallow; progress below)..."
if ! git clone --depth 1 --single-branch --branch "$MNGR_BRANCH" "$MNGR_URL" "$TMP/repo"; then
    echo "error: could not clone '$MNGR_BRANCH' from ${MNGR_URL} (branch name? gh auth?)." >&2
    exit 1
fi
UPSTREAM_SHA="$(git -C "$TMP/repo" rev-parse HEAD)"

# Tracked-file sets (relative paths), so we compare source, not caches/.venv.
git ls-files "$PREFIX" | sed "s|^$PREFIX/||" | sort > "$TMP/vendored.txt"
( cd "$TMP/repo" && git ls-files ) | sort > "$TMP/upstream.txt"
comm -23 "$TMP/upstream.txt" "$TMP/vendored.txt" > "$TMP/upstream_only.txt"   # new upstream (ignored)
comm -13 "$TMP/upstream.txt" "$TMP/vendored.txt" > "$TMP/vendored_only.txt"   # gone upstream (kept)

echo
echo ">>> updating vendored files that changed upstream"
# rsync --existing: update files that ALREADY exist under $PREFIX; never create, never delete.
# --checksum compares by CONTENT; we deliberately DROP time-sync (-rlpgoD, i.e. -a minus -t)
# so a fresh clone's all-new mtimes don't flag every identical file -- only real content
# diffs are transferred and reported. git ignores mtime anyway, so this changes nothing real.
RSYNC_FLAGS=(-rlpgoD --checksum --existing --exclude='.git/')
if [ "$DRY_RUN" -eq 1 ]; then
    rsync -in "${RSYNC_FLAGS[@]}" "$TMP/repo/" "$PREFIX/" > "$TMP/changes.txt" || true
    if [ -s "$TMP/changes.txt" ]; then
        echo "    would update:"; sed 's/^/      /' "$TMP/changes.txt"
    else
        echo "    (no vendored files differ from ${MNGR_BRANCH})"
    fi
else
    rsync "${RSYNC_FLAGS[@]}" "$TMP/repo/" "$PREFIX/"
    git -C "$REPO_ROOT" --no-pager diff --stat -- "$PREFIX" > "$TMP/diffstat.txt" || true
    if [ -s "$TMP/diffstat.txt" ]; then
        echo "    updated (in your working tree, unstaged):"; sed 's/^/      /' "$TMP/diffstat.txt"
    else
        echo "    (no vendored files differ from ${MNGR_BRANCH})"
    fi
fi

# Report the things we deliberately did NOT touch.
if [ -s "$TMP/upstream_only.txt" ]; then
    echo
    echo ">>> NOT added -- $(wc -l < "$TMP/upstream_only.txt") file(s) exist upstream but are not vendored here"
    echo "    (add by hand only if they belong in the vendored subset):"
    sed 's/^/      + /' "$TMP/upstream_only.txt" | head -n 40
    [ "$(wc -l < "$TMP/upstream_only.txt")" -gt 40 ] && echo "      ... (truncated)"
fi
if [ -s "$TMP/vendored_only.txt" ]; then
    echo
    echo ">>> kept -- $(wc -l < "$TMP/vendored_only.txt") file(s) vendored here but gone upstream (not deleted):"
    sed 's/^/      - /' "$TMP/vendored_only.txt" | head -n 40
    [ "$(wc -l < "$TMP/vendored_only.txt")" -gt 40 ] && echo "      ... (truncated)"
fi

# Optional commit (local only; there is no push anywhere in this script).
if [ "$DRY_RUN" -eq 0 ] && [ "$DO_COMMIT" -eq 1 ]; then
    if git diff --quiet -- "$PREFIX"; then
        echo; echo "nothing to commit."
    else
        do_it=1
        if [ "$ASSUME_YES" -eq 0 ]; then
            printf '\ncommit the re-vendor to your current branch? [type "yes"]: ' >&2
            read -r a </dev/tty || a=""; [ "$a" = "yes" ] || do_it=0
        fi
        if [ "$do_it" -eq 1 ]; then
            git add -- "$PREFIX"
            git commit -qm "$PREFIX: re-vendor from mngr-internal ${MNGR_BRANCH} @ ${UPSTREAM_SHA:0:12}"
            echo "committed re-vendor ($(git rev-parse --short HEAD))."
        else
            echo "left changes unstaged."
        fi
    fi
fi

echo
echo "done.$([ "$DRY_RUN" -eq 1 ] && echo ' (dry-run: working tree untouched)')"
