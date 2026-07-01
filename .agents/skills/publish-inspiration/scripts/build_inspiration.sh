#!/usr/bin/env bash
# Assemble a clean, shareable "inspiration" snapshot on top of the FCT base the
# mind was created from, then commit it. Run by the publish-inspiration
# launch-task worker on its ISOLATED worktree (cwd = worktree repo root).
#
# The dev `create-new-mind-repo` recipe is NOT available in the VM, so this is
# self-contained. It does the assembly + secret scan + manifest/thumbnail +
# /welcome rewrite + boot smoke-check + single commit. It does NOT create the
# GitHub repo or push -- the lead owns the popup, GitHub login, and push.
#
# Known-correct methods embedded here (a prior build got these wrong):
#   - Clean base via `git read-tree -u --reset` + `git clean -fdxq`, NEVER
#     `git checkout <ref> -- .` (which leaks the mind's whole committed tree,
#     incl. secrets). No upstream fetch/pull -- provenance link only.
#   - Overlay via `rsync -a "$STAGE/" "$REPO/"` (root-to-root), NEVER
#     `cp -a "$STAGE/apps" "$REPO/apps"` (nests into apps/apps).
#   - Secret scan is a hard-failing (exit-non-zero, abort-before-commit) gate on
#     token patterns and credential filenames -- the authoritative blocker.
#   - Boot smoke-check via the supervisor python lib (realize/process_config),
#     NEVER `supervisord -t` (in supervisord, -t means --strip_ansi and LAUNCHES
#     the daemon).

set -euo pipefail

# --- argument parsing --------------------------------------------------------

BASE_REF=""
SLUG=""
TITLE=""
DESCRIPTION=""
INCLUDE_PATHS=()
DATA_INCLUDE_PATHS=()

usage() {
    cat >&2 <<'USAGE'
Usage: build_inspiration.sh --base-ref <ref> --slug <slug> --title <title>
                            --include <path> [--include <path> ...]
                            [--data-include <path> ...] [--description <text>]
USAGE
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --base-ref)
            BASE_REF="${2:-}"
            shift 2
            ;;
        --slug)
            SLUG="${2:-}"
            shift 2
            ;;
        --title)
            TITLE="${2:-}"
            shift 2
            ;;
        --description)
            DESCRIPTION="${2:-}"
            shift 2
            ;;
        --include)
            INCLUDE_PATHS+=("${2:-}")
            shift 2
            ;;
        --data-include)
            DATA_INCLUDE_PATHS+=("${2:-}")
            shift 2
            ;;
        -h | --help)
            usage
            ;;
        *)
            echo "build_inspiration.sh: unknown argument: $1" >&2
            usage
            ;;
    esac
done

if [ -z "$BASE_REF" ] || [ -z "$SLUG" ] || [ -z "$TITLE" ]; then
    echo "build_inspiration.sh: --base-ref, --slug, and --title are required" >&2
    usage
fi
if [ "${#INCLUDE_PATHS[@]}" -eq 0 ]; then
    echo "build_inspiration.sh: at least one --include path is required" >&2
    usage
fi

# Validate the slug the same way the backend does: ^[A-Za-z0-9._-]+$ and no
# leading '-'. This names the manifest, thumbnail, and (via the caller) the repo.
if ! printf '%s' "$SLUG" | grep -Eq '^[A-Za-z0-9._-]+$' || case "$SLUG" in -*) true ;; *) false ;; esac; then
    echo "build_inspiration.sh: slug must match ^[A-Za-z0-9._-]+\$ and not start with '-': $SLUG" >&2
    exit 2
fi

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

MANIFEST="inspiration-${SLUG}.md"
THUMBNAIL="inspiration-${SLUG}.svg"

# --- 1. stage the selected paths out of the LIVE worktree BEFORE the reset ----

# rsync -R preserves each relative path so it lands at the same location under
# the stage dir; the reset in step 2 wipes the live paths, so we must capture
# them first. Also stage any pre-existing accumulated inspiration manifests +
# thumbnails so they carry forward (step 4).
STAGE="$(mktemp -d)"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

stage_one() {
    # Stage a single repo-root-relative path if it exists in the live worktree.
    local rel="$1"
    if [ -e "$rel" ]; then
        rsync -aR "$rel" "$STAGE/"
    else
        echo "build_inspiration.sh: warning: include path not found, skipping: $rel" >&2
    fi
}

for rel in "${INCLUDE_PATHS[@]}"; do
    stage_one "$rel"
done
for rel in "${DATA_INCLUDE_PATHS[@]}"; do
    stage_one "$rel"
done

# Carry forward any existing accumulated inspirations (manifest + sibling svg).
shopt -s nullglob
for existing in inspiration-*.md inspiration-*.svg; do
    rsync -aR "$existing" "$STAGE/"
done
shopt -u nullglob

# --- 2. clean base = the FCT version the mind was based on --------------------

# read-tree -u --reset makes the index+worktree match BASE_REF, dropping
# tracked-but-not-in-base files. clean -fdxq then drops untracked AND gitignored
# cruft (secrets, runtime state). This is the ONLY correct way to get a clean
# base -- `git checkout <ref> -- .` would leave the mind's whole tree in place.
# NO fetch/pull: BASE_REF is already a real commit in this repo's history.
git read-tree -u --reset "$BASE_REF"
git clean -fdxq

# --- 3. overlay the staged paths onto the clean base -------------------------

# Root-to-root contents merge. The trailing slash on the source is load-bearing:
# it merges the stage's CONTENTS into $REPO, so a path like apps/foo lands at
# apps/foo even when apps/ already exists on the base -- never nesting apps/apps.
rsync -a "$STAGE/" "$REPO/"

# --- 4. (carry-forward already handled in step 1's staging) ------------------

# --- 5. secret scan (authoritative, hard-failing blocker) --------------------

# Token patterns and credential filenames. A hit prints the offending path (and
# a redacted marker for value hits) and exits non-zero so the worker reports
# `stuck` and NOTHING is committed or pushed. This is the enforced gate on top
# of the .gitignore denylist -- not LLM prose.

scan_failed=0

# Files to scan: everything tracked-or-untracked under the assembled tree,
# excluding the .git dir. Use git to enumerate so we respect nothing and see
# every real file that would be committed.
mapfile -d '' ALL_FILES < <(find "$REPO" -type f -not -path "$REPO/.git/*" -print0)

# 5a. credential filenames (basename or path-suffix match).
CREDENTIAL_BASENAMES=(
    ".git-credentials"
    ".netrc"
    ".claude.json"
    ".sesskey"
    ".pypirc"
)
# Path-suffix credential locations (not just basename).
CREDENTIAL_SUFFIXES=(
    ".config/gh/hosts.yml"
)
for f in "${ALL_FILES[@]}"; do
    base="$(basename "$f")"
    for bad in "${CREDENTIAL_BASENAMES[@]}"; do
        if [ "$base" = "$bad" ]; then
            echo "build_inspiration.sh: SECRET SCAN FAILED: credential file present: ${f#"$REPO"/}" >&2
            scan_failed=1
        fi
    done
    for suffix in "${CREDENTIAL_SUFFIXES[@]}"; do
        case "$f" in
            *"$suffix")
                echo "build_inspiration.sh: SECRET SCAN FAILED: credential file present: ${f#"$REPO"/}" >&2
                scan_failed=1
                ;;
        esac
    done
    # .env / .env.* (but not .env.example / .env.sample templates, which are
    # deliberately non-secret; a bare .env or .env.<anything-else> is blocked).
    case "$base" in
        .env | .env.*)
            case "$base" in
                .env.example | .env.sample | .env.template) ;;
                *)
                    echo "build_inspiration.sh: SECRET SCAN FAILED: env file present: ${f#"$REPO"/}" >&2
                    scan_failed=1
                    ;;
            esac
            ;;
    esac
done

# 5b. token / key value patterns inside file contents.
# Patterns: GitHub tokens (ghp_, github_pat_, gho_), Anthropic keys (sk-ant-),
# AWS access key ids (AKIA + 16 upper alnum), and PEM private-key headers.
TOKEN_PATTERN='ghp_|github_pat_|gho_|sk-ant-|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
for f in "${ALL_FILES[@]}"; do
    # Skip binary files: grep -I treats them as non-matching, but we also want to
    # avoid huge assets. -I skips binary; -E enables the alternation.
    if grep -IEl -- "$TOKEN_PATTERN" "$f" >/dev/null 2>&1; then
        echo "build_inspiration.sh: SECRET SCAN FAILED: token/key pattern in: ${f#"$REPO"/} (value redacted)" >&2
        scan_failed=1
    fi
done

if [ "$scan_failed" -ne 0 ]; then
    echo "build_inspiration.sh: aborting before commit -- secret scan found credentials or tokens in the assembled tree" >&2
    exit 1
fi

# --- no-diff guard: nothing to publish beyond the base -----------------------

# If the assembled tree is identical to BASE_REF's tree, there is nothing to
# publish. Compare via git: stage everything, then diff the index tree against
# BASE_REF's tree. (This runs before manifest/thumbnail/welcome writes, which
# would themselves create a diff.)
git add -A
ASSEMBLED_TREE="$(git write-tree)"
BASE_TREE="$(git rev-parse "${BASE_REF}^{tree}")"
if [ "$ASSEMBLED_TREE" = "$BASE_TREE" ]; then
    echo "build_inspiration.sh: nothing to publish -- the selected apps/features add nothing beyond the base" >&2
    exit 3
fi

# --- 6. generate the manifest ------------------------------------------------

# Build a human-readable "Apps included" list from the include paths (grouped by
# top-level dir for a light touch; the worker can enrich the prose sections).
apps_included=""
for rel in "${INCLUDE_PATHS[@]}"; do
    apps_included+="- \`${rel}\`"$'\n'
done

manifest_description="$DESCRIPTION"
if [ -z "$manifest_description" ]; then
    manifest_description="A shareable snapshot of ${TITLE}."
fi

cat > "$MANIFEST" <<MANIFEST_EOF
---
title: ${TITLE}
description: ${manifest_description}
thumbnail: ${THUMBNAIL}
---

# ${TITLE}

## What it is
${manifest_description}

## Apps included
${apps_included}
## Holes
What is missing, stubbed, or must be wired by the adapter goes here (for
example, external integrations that ship as placeholders and need the adapter's
own account or channel).

## Permissions it may need
Tokens, scopes, or external accounts the adapter must supply go here (for
example, an API token with a specific scope).

## Adaptation history
MANIFEST_EOF

# --- 7. generate a placeholder thumbnail (mock data only) --------------------

# A neutral placeholder SVG using MOCK data only -- never real user data. The
# lead may overwrite this with the popup-confirmed, server-sanitized SVG.
cat > "$THUMBNAIL" <<THUMB_EOF
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 160" role="img" aria-label="${TITLE}">
  <rect width="240" height="160" rx="12" fill="#1f2933"/>
  <rect x="20" y="24" width="200" height="20" rx="6" fill="#3e4c59"/>
  <rect x="20" y="60" width="140" height="12" rx="6" fill="#52606d"/>
  <rect x="20" y="84" width="180" height="12" rx="6" fill="#52606d"/>
  <rect x="20" y="108" width="100" height="12" rx="6" fill="#52606d"/>
  <text x="20" y="150" font-family="sans-serif" font-size="11" fill="#9aa5b1">inspiration</text>
</svg>
THUMB_EOF

# --- 8. rewrite the /welcome stable region -----------------------------------

# Replace everything BETWEEN the two markers (exclusive of the markers
# themselves) with an inspiration-specific welcome that names this inspiration.
# Deterministic (awk on the two markers), never an LLM freeform edit. The
# markers, the "Welcome to Minds" opening, and the suggestions list are
# preserved.
WELCOME_FILE=".agents/skills/welcome/SKILL.md"
if [ -f "$WELCOME_FILE" ] \
    && grep -q '<!-- INSPIRATION:BEGIN -->' "$WELCOME_FILE" \
    && grep -q '<!-- INSPIRATION:END -->' "$WELCOME_FILE"; then
    NEW_REGION_FILE="$(mktemp)"
    cat > "$NEW_REGION_FILE" <<WELCOME_REGION_EOF

This project was created from the **${TITLE}** inspiration (slug: \`${SLUG}\`).
Its manifest is at \`inspiration-${SLUG}.md\`. To adapt it into this mind --
filling in its holes with the user -- follow the \`use-inspiration\` skill
(template path) on \`inspiration-${SLUG}.md\`.
WELCOME_REGION_EOF
    awk -v regionfile="$NEW_REGION_FILE" '
        /<!-- INSPIRATION:BEGIN -->/ {
            print
            while ((getline line < regionfile) > 0) print line
            close(regionfile)
            skip = 1
            next
        }
        /<!-- INSPIRATION:END -->/ {
            skip = 0
            print
            next
        }
        skip != 1 { print }
    ' "$WELCOME_FILE" > "${WELCOME_FILE}.tmp"
    mv "${WELCOME_FILE}.tmp" "$WELCOME_FILE"
    rm -f "$NEW_REGION_FILE"
else
    echo "build_inspiration.sh: warning: /welcome stable markers not found; skipping welcome rewrite" >&2
fi

# --- 9. boot smoke-check WITHOUT side effects, then single commit -------------

# Validate supervisord.conf via the supervisor python lib -- realize() +
# process_config() parse and check the config WITHOUT starting the daemon.
# NEVER `supervisord -t`: in supervisord, -t means --strip_ansi and LAUNCHES the
# daemon. If the lib is unavailable, skip the check (config holes in selected
# apps are acceptable; the base booting is what matters).
smoke_ok=1
if [ -f "supervisord.conf" ]; then
    if ! uv run python - <<'PYEOF'
import sys

try:
    from supervisor.options import ServerOptions
except Exception:
    # supervisor lib unavailable in this environment -- skip the check.
    sys.exit(0)

options = ServerOptions()
options.configfile = "supervisord.conf"
options.realize(args=[])
options.process_config(do_usage=False)
PYEOF
    then
        smoke_ok=0
    fi
fi
if [ "$smoke_ok" -ne 1 ]; then
    echo "build_inspiration.sh: boot smoke-check FAILED -- supervisord.conf did not realize cleanly" >&2
    exit 4
fi

# --- 10. single commit -------------------------------------------------------

# Record the provenance link to BASE_REF in the commit message. Do NOT add an
# upstream remote and do NOT fetch/pull -- parent.toml is a provenance link only.
git add -A
git commit -q -m "inspiration: ${SLUG}

Assembled on clean FCT base ${BASE_REF} (provenance link only; no upstream fetch)."

# --- 11. summary for the worker's done report --------------------------------

echo "build_inspiration.sh: assembled inspiration '${SLUG}' on clean base ${BASE_REF}"
echo "  included paths:"
for rel in "${INCLUDE_PATHS[@]}"; do
    echo "    - ${rel}"
done
if [ "${#DATA_INCLUDE_PATHS[@]}" -gt 0 ]; then
    echo "  data paths (opted in):"
    for rel in "${DATA_INCLUDE_PATHS[@]}"; do
        echo "    - ${rel}"
    done
fi
echo "  manifest:  ${MANIFEST}"
echo "  thumbnail: ${THUMBNAIL}"
echo "  boot smoke-check: passed"
