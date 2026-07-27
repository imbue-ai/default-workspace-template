#!/usr/bin/env bash
# Full cutover rehearsal against throwaway local repos: upstream drift, final
# sync, cutover commit, near-noop first run, steady-state exports with message
# scrubbing and tag creation. Nothing touches GitHub. See README.md.
# Usage: rehearse_cutover.sh <copybara_deploy.jar> <bare-public-seed.git> [--build]
set -euo pipefail

JAR="${1:?usage: rehearse_cutover.sh <copybara_deploy.jar> <bare-public-seed.git> [--build]}"
SEED="${2:?usage: rehearse_cutover.sh <copybara_deploy.jar> <bare-public-seed.git> [--build]}"
BUILD="${3:-}"
JAVA="${JAVA:-java}"
CONFIG="$(cd "$(dirname "$0")" && pwd)/copy.bara.sky"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" != "HEAD" ] || { echo "FAIL: detached HEAD; check out a branch first" >&2; exit 1; }
GITDIR="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"

WORK="$(mktemp -d)"
echo "workdir: $WORK (rehearsing committed state of branch $BRANCH)"

fail() {
    echo "FAIL: $1" >&2
    echo "workdir retained for debugging: $WORK" >&2
    exit 1
}

run_copybara() {
    local workflow="$1"; shift
    set +e
    "$JAVA" -jar "$JAR" migrate "$WORK/copy.bara.sky" "$workflow" \
        --output-root "$WORK/copybara-out" "$@" >> "$WORK/copybara.log" 2>&1
    local code=$?
    set -e
    # 4 = no-op, a success for our purposes
    if [ "$code" -ne 0 ] && [ "$code" -ne 4 ]; then
        tail -30 "$WORK/copybara.log" >&2
        fail "copybara $workflow failed with exit $code (full log: $WORK/copybara.log)"
    fi
    return 0
}

export GIT_AUTHOR_NAME="Rehearsal Bot"
export GIT_AUTHOR_EMAIL="dev@imbue.com"
export GIT_COMMITTER_NAME="Rehearsal Bot"
export GIT_COMMITTER_EMAIL="dev@imbue.com"

git clone --quiet --bare --single-branch --branch "$BRANCH" "$GITDIR" "$WORK/private.git"
cp -R "$SEED" "$WORK/public.git"

sed -e "s|https://github.com/imbue-ai/mngr-internal.git|file://$WORK/private.git|" \
    -e "s|https://github.com/imbue-ai/mngr.git|file://$WORK/public.git|" \
    -e "s|ref = \"main\"|ref = \"$BRANCH\"|" \
    "$CONFIG" > "$WORK/copy.bara.sky"
if grep -q "github.com" "$WORK/copy.bara.sky"; then
    fail "URL substitution incomplete: github.com still present in generated config"
fi
grep -q "file://$WORK/private.git" "$WORK/copy.bara.sky" || fail "origin URL not substituted"
grep -q "file://$WORK/public.git" "$WORK/copy.bara.sky" || fail "destination URL not substituted"

echo "[1/8] upstream public commit lands after the seed (simulated drift)"
git clone --quiet "file://$WORK/public.git" "$WORK/pubwork"
echo "upstream public change" >> "$WORK/pubwork/libs/mngr/README.md"
git -C "$WORK/pubwork" commit -qam "upstream: public change before cutover"
git -C "$WORK/pubwork" push -q origin main

echo "[2/8] final sync: merge public main into the private branch"
git clone --quiet -b "$BRANCH" "file://$WORK/private.git" "$WORK/checkout"
git -C "$WORK/checkout" fetch -q "file://$WORK/public.git" main
git -C "$WORK/checkout" merge -q --no-edit FETCH_HEAD
git -C "$WORK/checkout" push -q origin "$BRANCH"
MERGE_SHA="$(git -C "$WORK/checkout" rev-parse HEAD)"

echo "[3/8] cutover commit: public main restricted to the filtered tree"
run_copybara materialize --folder-dir "$WORK/materialized" --force
rsync -a --delete --exclude ".git" "$WORK/materialized/" "$WORK/pubwork/"
git -C "$WORK/pubwork" add -A
git -C "$WORK/pubwork" commit -qm "Restrict this repository to the open-source subset

Development continues in the private source-of-truth repository; this
repository is maintained as its public mirror."
git -C "$WORK/pubwork" push -q origin main
CUTOVER_SHA="$(git -C "$WORK/pubwork" rev-parse HEAD)"

echo "[4/8] probe commit, then first sync must reproduce exactly the filtered tree"
# A trivial public commit gives the first sync something to migrate, so the
# destination tree after it is copybara's own computation (a no-op run would
# prove nothing) and the run writes the first GitOrigin-RevId trailer.
echo "post-cutover probe" >> "$WORK/checkout/libs/mngr/README.md"
git -C "$WORK/checkout" commit -qam "post-cutover probe"
git -C "$WORK/checkout" push -q origin "$BRANCH"
run_copybara push --last-rev "$MERGE_SHA" --force
NEW="$(git -C "$WORK/public.git" rev-list --count "$CUTOVER_SHA..main")"
[ "$NEW" -eq 1 ] || fail "first sync after cutover exported $NEW commits; expected exactly the probe"
mkdir "$WORK/pubtree"
git -C "$WORK/public.git" archive main | tar -x -C "$WORK/pubtree"
run_copybara materialize --folder-dir "$WORK/materialized2" --force
diff -r "$WORK/pubtree" "$WORK/materialized2" > "$WORK/tree-diff.txt" 2>&1 \
    || { head -20 "$WORK/tree-diff.txt" >&2; fail "destination tree differs from the filtered origin tree"; }

echo "[5/8] steady-state private commits (scrub, block-strip, private-only, release)"
echo "retry fix" >> "$WORK/checkout/libs/mngr/README.md"
git -C "$WORK/checkout" commit -qam "Fix retry logic (#123)

INTERNAL: modal capacity notes, must not reach the mirror"
printf '\nBEGIN-INTERNAL\ninternal-only notes\nEND-INTERNAL\n' >> "$WORK/checkout/libs/imbue_common/README.md"
echo "public tweak" >> "$WORK/checkout/libs/imbue_common/README.md"
git -C "$WORK/checkout" commit -qam "Document imbue_common tweak"
echo "ops note" > "$WORK/checkout/specs/rehearsal-note.md"
git -C "$WORK/checkout" add -A
git -C "$WORK/checkout" commit -qm "private-only: ops note"
echo "release build" >> "$WORK/checkout/libs/mngr/README.md"
git -C "$WORK/checkout" commit -qam "Release v99.99.99 (rehearsal)

RELEASE_TAG=v99.99.99"
git -C "$WORK/checkout" push -q origin "$BRANCH"

echo "[6/8] export the batch flagless (the probe's trailer is the baseline)"
run_copybara push
EXPORTED="$(git -C "$WORK/public.git" rev-list --count "$CUTOVER_SHA..main")"
[ "$EXPORTED" -eq 4 ] || fail "expected 4 exported commits (probe + 3; private-only skipped), got $EXPORTED"

git -C "$WORK/public.git" log --format=%B "$CUTOVER_SHA..main" > "$WORK/export-log.txt"
grep -q "INTERNAL:" "$WORK/export-log.txt" && fail "INTERNAL: message section leaked to the mirror"
grep -q "mngr-internal#123" "$WORK/export-log.txt" || fail "#123 reference was not rewritten to mngr-internal#123"
grep -q "GitOrigin-RevId:" "$WORK/export-log.txt" || fail "GitOrigin-RevId trailer missing"
grep -qE "rehearsal-note|private-only" "$WORK/export-log.txt" && fail "private-only commit leaked"
git -C "$WORK/public.git" show main:libs/imbue_common/README.md > "$WORK/readme-content.txt"
grep -q "BEGIN-INTERNAL" "$WORK/readme-content.txt" && fail "BEGIN-INTERNAL block leaked into file content"
grep -q "public tweak" "$WORK/readme-content.txt" || fail "public part of the block-strip commit is missing"
git -C "$WORK/public.git" tag -l > "$WORK/tags.txt"
grep -qx "v99.99.99" "$WORK/tags.txt" || fail "RELEASE_TAG did not create tag v99.99.99 on the mirror"

echo "[7/8] trailer-based steady state (no flags at all)"
echo "post-release fix" >> "$WORK/checkout/libs/mngr/README.md"
git -C "$WORK/checkout" commit -qam "Post-release public fix"
git -C "$WORK/checkout" push -q origin "$BRANCH"
run_copybara push
git -C "$WORK/public.git" log -1 --format=%s main | grep -q "Post-release public fix" \
    || fail "flagless steady-state run did not export the new commit"

if [ "$BUILD" = "--build" ]; then
    echo "[8/8] public-buildability: uv sync --locked + pytest collect"
    git clone --quiet "file://$WORK/public.git" "$WORK/buildtree"
    (cd "$WORK/buildtree" \
        && uv sync --locked --all-packages \
        && uv run pytest --collect-only -q 2>&1 | tail -2) || fail "public tree buildability checks failed"
else
    echo "[8/8] skipped buildability checks (pass --build to include them)"
fi

echo "REHEARSAL PASS: drift merged; cutover tree exact; first sync reproduced the filtered tree bit-for-bit;"
echo "REHEARSAL PASS: scrubbing, block-strip, private-skip, tag creation, flagless trailer steady-state all verified."
rm -rf "$WORK"
