#!/usr/bin/env bash
# Materialize the filtered public tree from this repo's committed branch state.
# Usage: materialize_public_tree.sh <copybara_deploy.jar> <output-dir> [--lock] [--check]
#   --lock   regenerate mirror/overlay/uv.lock from the materialized tree
#   --check  run the public-buildability checks in the materialized tree
set -euo pipefail

JAR="${1:?usage: materialize_public_tree.sh <copybara_deploy.jar> <output-dir> [--lock] [--check]}"
OUT="${2:?usage: materialize_public_tree.sh <copybara_deploy.jar> <output-dir> [--lock] [--check]}"
shift 2
JAVA="${JAVA:-java}"
CONFIG="$(cd "$(dirname "$0")" && pwd)/copy.bara.sky"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# MATERIALIZE_REF overrides the ref to materialize (any fetchable ref, e.g. a
# temporary ref of an uncommitted tree); default is the checked-out branch.
if [ -n "${MATERIALIZE_REF:-}" ]; then
    REF="$MATERIALIZE_REF"
else
    REF="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
    [ "$REF" != "HEAD" ] || { echo "FAIL: detached HEAD; check out a branch first" >&2; exit 1; }
fi
GITDIR="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"

WORK="$(mktemp -d)"
echo "materializing committed state of $REF into $OUT"

# The destination URL is neutralized so even a mistaken `push` invocation of
# this generated config cannot reach the real public repo.
sed -e "s|https://github.com/imbue-ai/mngr-internal.git|file://$GITDIR|" \
    -e "s|https://github.com/imbue-ai/mngr.git|file://$WORK/no-such-destination.git|" \
    -e "s|ref = \"main\"|ref = \"$REF\"|" \
    "$CONFIG" > "$WORK/copy.bara.sky"
grep -q "file://$GITDIR" "$WORK/copy.bara.sky" || { echo "FAIL: origin URL not substituted" >&2; exit 1; }
grep -q "github.com" "$WORK/copy.bara.sky" && { echo "FAIL: a github.com URL survived substitution" >&2; exit 1; }

mkdir -p "$OUT"
if ! "$JAVA" -jar "$JAR" migrate "$WORK/copy.bara.sky" materialize \
    --folder-dir "$OUT" --output-root "$WORK/copybara-out" --force \
    > "$WORK/copybara.log" 2>&1; then
    tail -30 "$WORK/copybara.log" >&2
    echo "FAIL: materialize run failed (full log: $WORK/copybara.log)" >&2
    exit 1
fi

for flag in "$@"; do
    case "$flag" in
    --lock)
        # Seed from the private lock so the public workspace keeps the same
        # resolved versions (uv preserves satisfying pins and only prunes the
        # absent members); a fresh resolve would drift the mirror's deps away
        # from what private CI actually tests.
        cp "$ROOT/uv.lock" "$OUT/uv.lock"
        (cd "$OUT" && uv lock)
        cp "$OUT/uv.lock" "$ROOT/mirror/overlay/uv.lock"
        echo "regenerated mirror/overlay/uv.lock"
        ;;
    --check)
        # Import-lint violations are governed by the in-repo ratchet tests
        # (which tolerate a recorded count), so a bare lint-imports run is
        # deliberately not part of this gate.
        # Real consumers get the mirror via git clone; tests locate the repo
        # root by walking up to .git, so the check tree needs one too.
        git -C "$OUT" init -q
        (cd "$OUT" \
            && uv sync --locked --all-packages \
            && { uv run pytest --collect-only -q > collect.log 2>&1 \
                || { tail -60 collect.log >&2; exit 1; }; tail -2 collect.log; })
        echo "public-buildability checks passed"
        ;;
    *)
        echo "unknown flag: $flag" >&2
        exit 1
        ;;
    esac
done
rm -rf "$WORK"
