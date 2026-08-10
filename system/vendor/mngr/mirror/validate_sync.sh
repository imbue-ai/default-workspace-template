#!/usr/bin/env bash
# End-to-end validation of copy.bara.sky against throwaway local repos.
# Usage: validate_sync.sh <copybara_deploy.jar> <bare-seed-clone.git>
# See mirror/README.md.
set -euo pipefail

JAR="${1:?usage: validate_sync.sh <copybara_deploy.jar> <bare-seed-clone.git>}"
SEED="${2:?usage: validate_sync.sh <copybara_deploy.jar> <bare-seed-clone.git>}"
JAVA="${JAVA:-java}"
CONFIG="$(cd "$(dirname "$0")" && pwd)/copy.bara.sky"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" != "HEAD" ] || { echo "FAIL: detached HEAD; check out a branch first" >&2; exit 1; }
GITDIR="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"
echo "validating committed state of branch $BRANCH (uncommitted changes are not seen)"

WORK="$(mktemp -d)"
echo "workdir: $WORK"

fail() {
    echo "FAIL: $1" >&2
    echo "workdir retained for debugging: $WORK" >&2
    exit 1
}

export GIT_AUTHOR_NAME="Validation Bot"
export GIT_AUTHOR_EMAIL="dev@imbue.com"
export GIT_COMMITTER_NAME="Validation Bot"
export GIT_COMMITTER_EMAIL="dev@imbue.com"

# The private side is seeded from THIS repo's current branch (so the sync
# machinery, overlay, and justfile split are present in the origin); the
# public side is seeded from the real public repo's state.
git clone --quiet --bare --single-branch --branch "$BRANCH" "$GITDIR" "$WORK/private.git"
cp -R "$SEED" "$WORK/public.git"

git clone --quiet "file://$WORK/private.git" "$WORK/checkout"
FORK_POINT="$(git -C "$WORK/checkout" rev-parse HEAD)"
# Origin and destination no longer share history (the origin is this branch),
# so destination-side assertions use the destination's own pre-sync head.
PUB_BASE="$(git -C "$WORK/public.git" rev-parse main)"

echo "validation: public-only change" >> "$WORK/checkout/libs/mngr/README.md"
git -C "$WORK/checkout" add -A
git -C "$WORK/checkout" commit -q -m "validation: public-only change"
PUBLIC_ONLY_SHA="$(git -C "$WORK/checkout" rev-parse HEAD)"

echo "validation: private-only change" > "$WORK/checkout/specs/validation-scratch.md"
git -C "$WORK/checkout" add -A
git -C "$WORK/checkout" commit -q -m "validation: private-only change"

echo "validation: mixed change" >> "$WORK/checkout/libs/imbue_common/README.md"
echo "validation: mixed change" > "$WORK/checkout/blueprint/validation-scratch.md"
git -C "$WORK/checkout" add -A
git -C "$WORK/checkout" commit -q -m "validation: mixed change"
MIXED_SHA="$(git -C "$WORK/checkout" rev-parse HEAD)"

git -C "$WORK/checkout" push -q origin HEAD

sed -e "s|https://github.com/imbue-ai/mngr-internal.git|file://$WORK/private.git|" \
    -e "s|https://github.com/imbue-ai/mngr.git|file://$WORK/public.git|" \
    -e "s|ref = \"main\"|ref = \"$BRANCH\"|" \
    "$CONFIG" > "$WORK/copy.bara.sky"

# Refuse to run unless both URL substitutions verifiably happened: a silent
# sed no-op would point this run (which uses --force) at the real GitHub
# repos.
if grep -q "github.com" "$WORK/copy.bara.sky"; then
    fail "URL substitution incomplete: github.com still present in generated config"
fi
grep -q "file://$WORK/private.git" "$WORK/copy.bara.sky" || fail "origin URL not substituted"
grep -q "file://$WORK/public.git" "$WORK/copy.bara.sky" || fail "destination URL not substituted"

if ! "$JAVA" -jar "$JAR" migrate "$WORK/copy.bara.sky" push \
    --last-rev "$FORK_POINT" --force --output-root "$WORK/copybara-out" \
    > "$WORK/copybara.log" 2>&1; then
    tail -40 "$WORK/copybara.log" >&2
    fail "copybara run failed (full log: $WORK/copybara.log)"
fi

NEW_COUNT="$(git -C "$WORK/public.git" rev-list --count "$PUB_BASE..main")"
[ "$NEW_COUNT" -eq 2 ] || fail "expected 2 migrated commits on destination, got $NEW_COUNT"

SUBJECTS="$(git -C "$WORK/public.git" log --format=%s "$PUB_BASE..main")"
if echo "$SUBJECTS" | grep -q "private-only"; then
    fail "private-only commit leaked to destination"
fi

# Exact-tree check: the destination must contain precisely what the config's
# include list declares, at three granularities (repo root, libs/, scripts/).
# Expectations are parsed from the config so this cannot drift from it.
# Overlay entries land at the destination root (core.move), so map them
# before deriving expectations.
INCLUDES_FILE="$WORK/config-includes.txt"
sed -n '/include = \[/,/\]/p' "$CONFIG" | grep -oE '"[^"]+"' | tr -d '"' \
    | sed -e 's|^mirror/overlay/||' > "$INCLUDES_FILE"
[ -s "$INCLUDES_FILE" ] || fail "could not parse include list from config"

sed -e 's|/.*||' "$INCLUDES_FILE" | sort -u > "$WORK/expected-top.txt"
grep "^libs/" "$INCLUDES_FILE" | sed -e 's|^libs/||' -e 's|/\*\*$||' | sort -u > "$WORK/expected-libs.txt"
grep "^apps/" "$INCLUDES_FILE" | sed -e 's|^apps/||' -e 's|/\*\*$||' | sort -u > "$WORK/expected-apps.txt"
grep "^scripts/" "$INCLUDES_FILE" | sed -e 's|^scripts/||' | sort -u > "$WORK/expected-scripts.txt"

git -C "$WORK/public.git" ls-tree --name-only main | sort > "$WORK/actual-top.txt"
git -C "$WORK/public.git" ls-tree --name-only main:libs | sort > "$WORK/actual-libs.txt"
git -C "$WORK/public.git" ls-tree --name-only main:apps | sort > "$WORK/actual-apps.txt"
git -C "$WORK/public.git" ls-tree --name-only main:scripts | sort > "$WORK/actual-scripts.txt"

diff -u "$WORK/expected-top.txt" "$WORK/actual-top.txt" >&2 || fail "destination repo root does not match the config include list"
diff -u "$WORK/expected-libs.txt" "$WORK/actual-libs.txt" >&2 || fail "destination libs/ does not match the config include list"
diff -u "$WORK/expected-apps.txt" "$WORK/actual-apps.txt" >&2 || fail "destination apps/ does not match the config include list"
diff -u "$WORK/expected-scripts.txt" "$WORK/actual-scripts.txt" >&2 || fail "destination scripts/ does not match the config include list"

# Every configured exclude must be absent from the destination, at any depth.
sed -n '/exclude = \[/,/\]/p' "$CONFIG" | grep -oE '"[^"]+"' | tr -d '"' | sed 's|/\*\*$||' > "$WORK/config-excludes.txt"
git -C "$WORK/public.git" ls-tree -r --name-only main > "$WORK/destination-tree.txt"
while IFS= read -r p; do
    # Literal prefix match (awk index), so glob metacharacters in config
    # entries cannot degrade this into a vacuous regex.
    if awk -v p="$p" 'index($0, p) == 1 { found = 1; exit } END { exit !found }' "$WORK/destination-tree.txt"; then
        fail "excluded path present in destination tree: $p"
    fi
done < "$WORK/config-excludes.txt"

MIXED_DEST="$(git -C "$WORK/public.git" log --format="%H %s" "$PUB_BASE..main" | awk '/mixed change/{print $1}')"
[ -n "$MIXED_DEST" ] || fail "mixed-change commit missing from destination"
MIXED_FILES="$(git -C "$WORK/public.git" show --name-only --format= "$MIXED_DEST")"
[ "$MIXED_FILES" = "libs/imbue_common/README.md" ] || fail "mixed commit diff is not exactly its public file: [$MIXED_FILES]"

FIRST_DEST="$(git -C "$WORK/public.git" log --format="%H %s" "$PUB_BASE..main" | awk '/public-only change/{print $1}')"
[ -n "$FIRST_DEST" ] || fail "public-only commit missing from destination"
FIRST_TRAILER="$(git -C "$WORK/public.git" log --format=%B -1 "$FIRST_DEST" | awk '/^GitOrigin-RevId:/{print $2}')"
[ "$FIRST_TRAILER" = "$PUBLIC_ONLY_SHA" ] || fail "GitOrigin-RevId on public-only commit is [$FIRST_TRAILER], expected $PUBLIC_ONLY_SHA"
MIXED_TRAILER="$(git -C "$WORK/public.git" log --format=%B -1 "$MIXED_DEST" | awk '/^GitOrigin-RevId:/{print $2}')"
[ "$MIXED_TRAILER" = "$MIXED_SHA" ] || fail "GitOrigin-RevId on mixed commit is [$MIXED_TRAILER], expected $MIXED_SHA"

echo "PASS: destination tree matches allowlist; private-only commit skipped;"
echo "PASS: mixed commit exported only its public diff; GitOrigin-RevId trailers correct."
rm -rf "$WORK"
