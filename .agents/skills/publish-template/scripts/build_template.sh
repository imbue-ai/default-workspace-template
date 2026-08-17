#!/usr/bin/env bash
# Assemble a clean, shareable "template" snapshot on top of the DEFAULT_WORKSPACE_TEMPLATE base the
# mind was created from, then commit it. Run by the launch-task WORKER the
# publish-template skill dispatches, from the worker's own git worktree
# (cwd = worktree repo root); the live mind's /home/user/workspace is never touched. This is
# v2 of the templates flow (see TEMPLATE_FLOW_VERSION below); the
# generated manifest records it as `format: v2` in its front-matter and in the
# sibling template.toml.
#
# The dev `create-new-mind-repo` recipe is NOT available in the VM, so this is
# self-contained. It does the assembly + secret scan + manifest/thumbnail +
# /welcome rewrite + version-history removal + boot smoke-check + single commit.
# It does NOT create the
# GitHub repo or push, and it deliberately leaves two things unfinished for the
# worker to complete before reporting done: the manifest's FILL-IN blocks (real
# prose) and the placeholder thumbnail (a bespoke, app-specific SVG). The lead
# owns the chat confirmation, GitHub login, and push.
#
# Known-correct methods embedded here (a prior build got these wrong):
#   - Clean base via `git read-tree -u --reset` + `git clean -fdxq`, NEVER
#     `git checkout <ref> -- .` (which leaks the mind's whole committed tree,
#     incl. secrets). No upstream fetch/pull -- provenance link only.
#   - Overlay via `rsync -a "$STAGE/" "$REPO/"` (root-to-root), NEVER
#     `cp -a "$STAGE/apps" "$REPO/apps"` (nests into apps/apps).
#   - Secret scan is a hard-failing (exit-non-zero, abort-before-commit) gate
#     -- the authoritative blocker. It runs the sibling scan_secrets.sh, which
#     requires BOTH scanners (betterleaks with the sibling betterleaks.toml
#     config, kingfisher with --no-validate) and fails on any finding, any
#     scanner error, or any missing scanner binary. There is NO fallback
#     scanner: the binaries are baked into the workspace image, so a missing
#     one is a broken environment.
#   - Boot smoke-check via the supervisor python lib (realize/process_config),
#     NEVER `supervisord -t` (in supervisord, -t means --strip_ansi and LAUNCHES
#     the daemon).
#
# Exit codes: 0 = success; 1 = secret scan hit OR a required scanner was
# missing/errored; 2 = usage error; 3 = nothing to publish beyond the base;
# 4 = boot smoke-check failed; 5 = --base-ref does not resolve to a bootable
# template tree; 6 = the generated manifest failed validation, or a declared
# apt package does not resolve in the pinned snapshot mirror.

set -euo pipefail

# Version of the templates flow (and of the manifest format this script
# writes into the generated manifest's `format:` front-matter key).
#
# v2 is the split format: one slug-free template.md/.toml/.svg per repo
# (overriding rather than accumulating, with a [[lineage]] chain recording what
# was superseded), the recipe and requirements moved into the TOML, an
# [environment] declaration, and Holes + Prerequisites merged into one
# Requirements list whose entries carry their own kind. v1 is the
# original: slug-named inspiration-<slug>.md with a YAML recipe block inside it
# and no TOML at all. Adopters still read v1 -- absence of template.toml is
# what identifies it -- but nothing writes v1 any more.
TEMPLATE_FLOW_VERSION="v2"

# The published version of THIS template (front-matter `version:`), distinct
# from the flow/manifest-format version above. A first publish is always v1; a
# later update of the same template publishes v2, v3, ... and the source
# workspace's docs/VERSION_HISTORY.md counts them.
TEMPLATE_VERSION="v1"

# Resolve this script's own directory up front, before any cd: the sibling
# scan_secrets.sh + betterleaks.toml live next to this script, and the script
# is invoked by a path that may be relative to the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- argument parsing --------------------------------------------------------

BASE_REF=""
SLUG=""
TITLE=""
DESCRIPTION=""
INCLUDE_PATHS=()
DATA_INCLUDE_PATHS=()

usage() {
    cat >&2 <<'USAGE'
Usage: build_template.sh --base-ref <ref> --slug <slug> --title <title>
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
            echo "build_template.sh: unknown argument: $1" >&2
            usage
            ;;
    esac
done

if [ -z "$BASE_REF" ] || [ -z "$SLUG" ] || [ -z "$TITLE" ]; then
    echo "build_template.sh: --base-ref, --slug, and --title are required" >&2
    usage
fi
if [ "${#INCLUDE_PATHS[@]}" -eq 0 ]; then
    echo "build_template.sh: at least one --include path is required" >&2
    usage
fi

# Validate the slug the same way the backend does: ^[A-Za-z0-9._-]+$ and no
# leading '-'. This names the manifest, thumbnail, and (via the caller) the repo.
if ! printf '%s' "$SLUG" | grep -Eq '^[A-Za-z0-9._-]+$' || case "$SLUG" in -*) true ;; *) false ;; esac; then
    echo "build_template.sh: slug must match ^[A-Za-z0-9._-]+\$ and not start with '-': $SLUG" >&2
    exit 2
fi

REPO="$(cd "$(git rev-parse --show-toplevel)" && pwd -P)"
cd "$REPO"

# --- refuse to run anywhere but a throwaway linked worktree ------------------
#
# Step 2 resets the tree to BASE_REF and runs `git clean -fdxq`, which deletes
# untracked AND gitignored files. In a live mind that is data/, .mngr/, the
# secrets, and every scrap of runtime state: an unrecoverable wipe of the
# user's workspace, by a script that believes it is doing its job.
#
# The skill's CWD invariant says assembly runs with cwd = $WT. That is an
# instruction, and an instruction cannot stop the delete on the run where it is
# not followed. This can, so the check lives here rather than only in prose.
LIVE_WORKSPACE="$(cd "${ENV_CONVERGE_WORKSPACE_DIR:-/home/user/workspace}" 2>/dev/null && pwd -P || true)"
if [ -n "$LIVE_WORKSPACE" ] && [ "$REPO" = "$LIVE_WORKSPACE" ]; then
    echo "build_template.sh: refusing to run in $REPO -- that is the live workspace." >&2
    echo "  Assembly resets the tree and deletes gitignored files (data/, .mngr/, secrets)." >&2
    echo "  Run it in a throwaway worktree instead: git worktree add \"\$WT\" <base-ref>" >&2
    exit 2
fi
# A linked worktree keeps its git dir at <common>/worktrees/<name>; in a main
# worktree the two resolve to the same path.
GIT_DIR_PATH="$(cd "$(git rev-parse --absolute-git-dir)" && pwd -P)"
GIT_COMMON_DIR_PATH="$(cd "$(git rev-parse --git-common-dir)" && pwd -P)"
if [ "$GIT_DIR_PATH" = "$GIT_COMMON_DIR_PATH" ]; then
    echo "build_template.sh: refusing to run in $REPO -- this is a repo's MAIN worktree." >&2
    echo "  Assembly resets the tree to the base and deletes untracked and gitignored files," >&2
    echo "  so it must run somewhere disposable." >&2
    echo "  Create one and run there: git worktree add \"\$WT\" <base-ref>" >&2
    exit 2
fi

# One template per repo: the manifest files carry no slug and a new publish
# OVERRIDES whatever was here rather than accumulating beside it. What survives
# an override is the [[lineage]] chain in the TOML -- each predecessor's repo
# URL and commit -- so a superseded manifest stays retrievable in the repo where
# it is authoritative. The slug is still the identity (it names the repo and
# keys the version ledger); it just no longer names any file.
MANIFEST="template.md"
MANIFEST_TOML="template.toml"
THUMBNAIL="template.svg"

# Substituted by the lead in publish-template §7 once the owner and repo name
# are both known -- the README's "Open in Minds" button and its copyable
# fallback both need the repo URL, which does not exist when this script runs.
# §8's pre-push gate greps for any leftover, exactly as it does for the
# placeholder thumbnail.
REPO_URL_PLACEHOLDER="MINDS_TEMPLATE_REPO_URL"

# --- 0. validate that BASE_REF is a real, bootable default workspace template tree ---------

# Guard against a wrong --base-ref: minds assembled via subtree merges can have
# several parallel root commits, and a naive fallback can land on a near-empty
# one instead of the real DEFAULT_WORKSPACE_TEMPLATE seed. Any bootable template tree must contain
# pyproject.toml and system/supervisord.conf, so require both in BASE_REF's tree. This
# runs BEFORE the destructive read-tree in step 2 so a bad ref aborts cleanly
# without touching the worktree.
if ! git rev-parse --verify --quiet "${BASE_REF}^{tree}" > /dev/null; then
    echo "build_template.sh: BASE REF INVALID: '${BASE_REF}' does not resolve to a tree in this repo" >&2
    exit 5
fi
base_missing=""
for required in pyproject.toml system/supervisord.conf; do
    if [ -z "$(git ls-tree --name-only "${BASE_REF}^{tree}" -- "$required")" ]; then
        base_missing="${base_missing} ${required}"
    fi
done
if [ -n "$base_missing" ]; then
    echo "build_template.sh: BASE REF INVALID: the tree of '${BASE_REF}' is missing:${base_missing}" >&2
    echo "build_template.sh: '${BASE_REF}' does not look like a bootable default-workspace-template base (a wrong root commit from a subtree merge?) -- pass the real DEFAULT_WORKSPACE_TEMPLATE seed commit as --base-ref" >&2
    exit 5
fi

# --- 1. stage the selected paths out of the LIVE worktree BEFORE the reset ----

# rsync -R preserves each relative path so it lands at the same location under
# the stage dir; the reset in step 2 wipes the live paths, so we must capture
# them first. Also stage any pre-existing accumulated template manifests +
# thumbnails so they carry forward (step 4).
STAGE="$(mktemp -d)"
SCAN_TOOLS_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$STAGE" "$SCAN_TOOLS_DIR"
}
trap cleanup EXIT

# Snapshot the secret-scan script + its betterleaks config OUT of the worktree
# before step 2's reset: BASE_REF may predate them, in which case the
# read-tree would delete them from the worktree before step 5's scan runs.
# There is no fallback scanner, so both files are REQUIRED -- abort now,
# before any destructive step, if either is missing. Copied side by side so
# scan_secrets.sh finds its sibling config without a --config flag.
for scan_file in scan_secrets.sh betterleaks.toml; do
    if [ ! -f "$SCRIPT_DIR/$scan_file" ]; then
        echo "build_template.sh: $SCRIPT_DIR/$scan_file is missing (required for the secret scan; no fallback exists); aborting before touching the worktree" >&2
        exit 1
    fi
    cp "$SCRIPT_DIR/$scan_file" "$SCAN_TOOLS_DIR/"
done

# Same problem, same fix, for the manifest tooling: the reset would remove these
# from the worktree (and takes .venv with it, which is why the validator runs
# under `uv run --no-project` against a snapshotted copy of the schema module
# rather than importing it from the workspace).
for tool_file in write_template_manifest.py validate_template.py; do
    if [ ! -f "$SCRIPT_DIR/$tool_file" ]; then
        echo "build_template.sh: $SCRIPT_DIR/$tool_file is missing (required to generate and validate the manifest); aborting before touching the worktree" >&2
        exit 1
    fi
    cp "$SCRIPT_DIR/$tool_file" "$SCAN_TOOLS_DIR/"
done
SCHEMA_MODULE="$REPO/system/services/env_converge/src/env_converge/template_manifest.py"
if [ ! -f "$SCHEMA_MODULE" ]; then
    echo "build_template.sh: $SCHEMA_MODULE is missing (the manifest schema; no fallback exists); aborting before touching the worktree" >&2
    exit 1
fi
cp "$SCHEMA_MODULE" "$SCAN_TOOLS_DIR/"

# Stage the manifest this publish will OVERRIDE, before the reset removes it.
# Its identity plus [origin] become the newest lineage entry, and its own
# lineage carries through ahead of that. Any v1 slug-named manifests are NOT
# carried forward -- superseding them is the point -- but a v1 manifest has no
# [origin], so it contributes no link and the lead records it by hand if it
# matters.
PREVIOUS_MANIFEST_TOML=""
if [ -f "$MANIFEST_TOML" ]; then
    PREVIOUS_MANIFEST_TOML="$SCAN_TOOLS_DIR/previous-template.toml"
    cp "$MANIFEST_TOML" "$PREVIOUS_MANIFEST_TOML"
fi

stage_one() {
    # Stage a single repo-root-relative path if it exists in the live worktree.
    local rel="$1"
    if [ -e "$rel" ]; then
        rsync -aR "$rel" "$STAGE/"
    else
        echo "build_template.sh: warning: include path not found, skipping: $rel" >&2
    fi
}

for rel in "${INCLUDE_PATHS[@]}"; do
    stage_one "$rel"
done
for rel in "${DATA_INCLUDE_PATHS[@]}"; do
    stage_one "$rel"
done

# Nothing to carry forward: a publish OVERRIDES the previous manifest rather
# than accumulating beside it, and the reset in step 2 drops the old files with
# everything else. The predecessor's address survives as a [[lineage]] entry
# (staged above), which is what makes the override non-destructive.

# --- 2. clean base = the DEFAULT_WORKSPACE_TEMPLATE version the mind was based on --------------------

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

# The scan is the snapshotted scan_secrets.sh (with its sibling
# betterleaks.toml) over the STAGING dir. It runs TWO scanners --
# betterleaks, kingfisher (--no-validate) -- and exits non-zero on a finding
# from EITHER of them, on any scanner error, or on any missing scanner binary
# (no fallback: the binaries are baked into the workspace image, so a missing
# one is a broken environment, never a reason to scan less). A hit
# prints the offending path (value redacted, never printed) and this script
# exits 1, so the worker reports `stuck` and NOTHING is committed or pushed.
# This is the enforced gate on top of the .gitignore denylist -- not LLM
# prose.
#
# Scanning the STAGE (not the assembled tree) means the scan covers exactly
# the content overlaid out of the live mind: the selected --include /
# --data-include paths. The manifest files are generated after the scan. The
# clean base is the trusted, public default workspace template -- it cannot
# contain the user's secrets, and its own test fixtures legitimately hold
# placeholder token strings (e.g. "sk-ant-test"), so scanning it would only
# produce false positives that block every publish. The real risk is a secret
# riding in from the live mind's overlaid paths, and that is exactly what the
# stage holds. It also keeps the scan cheap regardless of how large the base
# is (never traverses vendor/, the base's fixtures, etc.). rsync -aR staged
# every file at its repo-relative path, so scan_secrets.sh's
# relative-to-scanned-dir finding paths ARE repo-root-relative.

if ! bash "$SCAN_TOOLS_DIR/scan_secrets.sh" "$STAGE"; then
    echo "build_template.sh: aborting before commit -- the secret scan found credentials/tokens in the overlaid content, or a required scanner was missing or failed (see above)" >&2
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
    echo "build_template.sh: nothing to publish -- the selected apps/features add nothing beyond the base" >&2
    exit 3
fi

# --- 6. generate the manifest ------------------------------------------------

# The manifest is the single document the NEXT agent (in a mind created from
# this template) reads to understand, present, and adapt the template.
# The deterministic parts (front-matter, included-path list, the "How to adapt
# it" script, section skeletons) are generated here; the prose that requires
# knowledge of the live mind is left as clearly-marked FILL-IN blocks that the
# worker MUST replace before reporting done.

# Human-readable list of what the snapshot includes, derived from the include
# paths (data includes are labeled as such).
included_paths_block=""
for rel in "${INCLUDE_PATHS[@]}"; do
    included_paths_block+="- \`${rel}\`"$'\n'
done
for rel in "${DATA_INCLUDE_PATHS[@]}"; do
    included_paths_block+="- \`${rel}\` (data, explicitly opted in)"$'\n'
done

manifest_description="$DESCRIPTION"
if [ -z "$manifest_description" ]; then
    manifest_description="A shareable snapshot of ${TITLE}."
fi

# The manifest's front matter is YAML, and title/description are the user's own
# words -- a title like `The "Daily" Digest: v2` breaks a bare scalar (a leading
# quote, or a `: `, changes how YAML parses the line). Emit every interpolated
# value as a double-quoted scalar instead: JSON string syntax is a valid subset
# of YAML's double-quoted style, so json.dumps does the escaping correctly.
# Bare python3 is fine here -- json is stdlib in every version, unlike tomllib.
yaml_scalar() {
    python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}
title_yaml="$(yaml_scalar "$TITLE")"
description_yaml="$(yaml_scalar "$manifest_description")"
thumbnail_yaml="$(yaml_scalar "$THUMBNAIL")"

# The RECIPE now lives in template.toml, not in this markdown. It is
# machine-read (only the publisher's own update flow reads it, and that flow
# always runs on a template new enough to know the TOML), it was the last YAML
# in the repo, and it is exactly the kind of structured data the split exists to
# get out of prose. Generate it with the manifest writer below.
manifest_toml_args=(
    --slug "$SLUG"
    --title "$TITLE"
    --version "$TEMPLATE_VERSION"
    --format "$TEMPLATE_FLOW_VERSION"
    --thumbnail "$THUMBNAIL"
    --output "$MANIFEST_TOML"
)
for rel in "${INCLUDE_PATHS[@]}"; do
    manifest_toml_args+=(--include "$rel")
done
for rel in "${DATA_INCLUDE_PATHS[@]}"; do
    manifest_toml_args+=(--data-include "$rel")
done
if [ -n "$PREVIOUS_MANIFEST_TOML" ]; then
    manifest_toml_args+=(--previous-manifest "$PREVIOUS_MANIFEST_TOML")
fi
manifest_toml_args+=(--description "$manifest_description")

# `uv run --no-project` (no workspace resolution, so none of the cold-base
# fragility the smoke check warns about) rather than a bare python3: the writer
# reads the previous manifest with tomllib, which is 3.11+, and the host's
# python3 is not guaranteed to be that new. uv supplies a managed interpreter,
# which makes this work the same everywhere instead of failing only on older
# machines.
if ! uv run --no-project python "$SCAN_TOOLS_DIR/write_template_manifest.py" "${manifest_toml_args[@]}"; then
    echo "build_template.sh: could not generate ${MANIFEST_TOML}" >&2
    exit 6
fi

cat > "$MANIFEST" <<MANIFEST_EOF
---
title: ${title_yaml}
description: ${description_yaml}
thumbnail: ${thumbnail_yaml}
version: ${TEMPLATE_VERSION}
format: ${TEMPLATE_FLOW_VERSION}
---

# ${TITLE}

This file is the manifest for the **${TITLE}** template (slug:
\`${SLUG}\`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a mind that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

${manifest_description}

<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment
with a one-paragraph overview of what this template does for its user: the
problem it solves, the main things it produces (pages, reports, automations),
and what the user sees when it is running. Write for a reader who has never
seen the original mind. -->

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean default-workspace-template base):

${included_paths_block}
<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment
with prose that makes the list above self-explanatory: for each included path,
say what it is (an app or lib with code, a skill, data) and what role it plays.
Then describe how the pieces wire together at runtime: which supervisord
programs (in system/supervisord.conf) run them, which ports they listen on and how
those are registered in forward_port.py (if applicable), and any scripts or
services that connect them. -->

## Recipe

This template is version \`${TEMPLATE_VERSION}\`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[\`${MANIFEST_TOML}\`](${MANIFEST_TOML}) -- its \`[recipe]\` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting mind must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable \`requires_\` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.

<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment
with both kinds of entry.

ACTIVATION -- one line each, using exactly these forms (greppable by \`requires_\`):

- requires_permission: <latchkey scope> / <permission schema> (user-approved;
  the adopting agent initiates this via a latchkey permission request during
  setup -- it must not merely mention it)
- requires_secret: <ENV_VAR or config key> (what it is for and where to put it)
- requires_llm: <how the code reaches Claude, and what an adopter needs>
  (include this line whenever the app calls an LLM: name the method it was
  built for -- keyed litellm via ANTHROPIC_API_KEY, or keyless subscription via
  claude -p -- so an adopter on the other method knows to switch it per the
  use-ai-integration skill)

Derive the real values from the included code (e.g. every service the app
calls through \`latchkey curl\`, and whether any code calls an LLM). Example:
- requires_permission: slack-api / slack-read-all (user-approved; adopting
  agent initiates during setup)
- requires_llm: calls Claude via the keyed litellm path (ANTHROPIC_API_KEY set);
  an adopter on the keyless subscription path must switch the model calls per
  use-ai-integration

These lines are what the ADOPTING agent acts on during setup, so a vague or
missing one silently breaks adoption -- a real incident: an adopter was never
prompted for a Slack permission the app needed. They are also what the lead
surfaces back to the publishing user for confirmation, so the list must be
complete and accurate. EVERY line must have its counterpart in
\`${MANIFEST_TOML}\`'s \`[requirements]\` (\`[[requirements.permission]]\`,
\`[[requirements.secret]]\`, \`[requirements.llm]\`); the validator compares
them and fails the publish if they disagree.

ADAPTATION -- one bullet each, in plain prose: every gap the adapter must
decide or rewire (stubbed integrations, hardcoded accounts/channels/ids, data
that was not included, anything that will not work out of the box). For each,
say what is missing and what a working replacement looks like. Mirror them as
\`[[requirements.adaptation]]\` entries in the TOML.

Do not repeat the README's "Ideas for making it yours" here -- those are
optional invitations, these are things that must be resolved.

If there is genuinely nothing of either kind, write exactly: "No requirements --
runs as published, with no external permissions or secrets." -->

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in \`${MANIFEST_TOML}\`'s \`[environment]\` table; an adopting mind
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that mind's environment rather than frozen to
whatever this publisher happened to have.

<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment
with a plain-language summary of what gets installed and why -- one line per
thing, naming what needs it (e.g. "poppler-utils: the digest renders PDF
attachments to text"). Fill in the matching entries in ${MANIFEST_TOML}'s
[environment] table at the same time; that table is what actually installs
anything, and this prose is what a human reads.

Derive it from the included code, not from what happens to be installed on this
machine: every binary the code shells out to, every global npm/uv/cargo tool it
invokes. If it needs nothing beyond the template's own environment, write
exactly: "Nothing extra -- runs on the stock workspace environment." -->

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new mind. This is the \`use-template\` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   \`requires_\` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every \`requires_permission\` line NOW
   via a latchkey permission request (see the \`latchkey\` skill; the request
   opens the approval/login flow in the minds app), wire up any
   \`requires_secret\` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment with
the first entry, in the form:
### v1 (YYYY-MM-DD) -- <one line: what this first version publishes>
using today's date. A later update of this template (the update-published-template
flow) appends "### v2 (date) -- what changed since v1", and so on. -->

## Adaptation history

Each mind that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
MANIFEST_EOF

# --- 7. generate a placeholder thumbnail (mock data only) --------------------

# A neutral placeholder SVG using MOCK data only -- never real user data. The
# marker comment makes "placeholder still in place" a deterministic grep: the
# worker MUST replace this whole file with a bespoke, app-specific SVG before
# reporting done, and the lead's pre-push gate blocks on the marker.
cat > "$THUMBNAIL" <<THUMB_EOF
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 160" role="img" aria-label="${TITLE}">
  <!-- minds-placeholder-thumbnail: replace with a bespoke SVG before publishing -->
  <rect width="240" height="160" rx="12" fill="#1f2933"/>
  <rect x="20" y="24" width="200" height="20" rx="6" fill="#3e4c59"/>
  <rect x="20" y="60" width="140" height="12" rx="6" fill="#52606d"/>
  <rect x="20" y="84" width="180" height="12" rx="6" fill="#52606d"/>
  <rect x="20" y="108" width="100" height="12" rx="6" fill="#52606d"/>
  <text x="20" y="150" font-family="sans-serif" font-size="11" fill="#9aa5b1">template</text>
</svg>
THUMB_EOF

# --- 8. write the template-specific /welcome into the SNAPSHOT ------------

# The published repo ships its OWN welcome skill, generated here by overwriting
# .agents/skills/welcome/SKILL.md in the assembled tree. The TEMPLATE's welcome
# skill is deliberately untouched by the templates feature -- no marker
# region, no takeover branch; the template handles changing the welcome
# entirely within the snapshot it publishes. Deterministic full-file write,
# never an LLM freeform edit; idempotent across accumulated publishes (each
# publish regenerates it targeting the newly-published slug, the latest).
welcome_description_yaml="$(yaml_scalar "Greet the user when a new project starts. This mind was created from the ${TITLE} template, so the welcome introduces that template and immediately starts the adaptation conversation.")"
WELCOME_FILE=".agents/skills/welcome/SKILL.md"
mkdir -p "$(dirname "$WELCOME_FILE")"
cat > "$WELCOME_FILE" <<WELCOME_EOF
---
name: welcome
description: ${welcome_description_yaml}
---

# Welcome the user (template: ${TITLE})

This mind was created from a template -- a published snapshot of apps
another mind built:

- Title: ${TITLE}
- Slug: \`${SLUG}\`
- Description: ${manifest_description}
- Manifest: \`${MANIFEST}\` (at the repo root, with \`${MANIFEST_TOML}\` beside it)

Do ALL of the following in your FIRST response, in the same turn, without
waiting to be asked:

1. Open with a short CUSTOM welcome that names **${TITLE}** and gives the
   one-line description above. Do NOT use a generic "Welcome to Minds"
   greeting and do NOT offer a generic suggestions list.
2. Immediately read \`${MANIFEST}\` at the repo root (reading the
   manifest in the first turn is required).
3. In plain, non-technical language, present what the template is and
   what it needs from the user -- name the manifest's activation requirements
   (the connectors/permissions it runs on). Then ask whether they want to hook it
   up to their own accounts now (e.g. "Want me to connect this to your own
   Slack?"). End your first response on THAT question. This is the
   \`use-template\` skill's template path; the manifest's "How to adapt
   it" section is the full script: if they say yes, ACTIVATE FIRST -- initiate
   each \`requires_permission\` via a latchkey permission request, get the
   app showing THEIR OWN DATA (that is the definition of working; a running
   service is not), invite them to take a look -- and only then ask how they
   want to adapt it.

This repo holds exactly one template. If \`${MANIFEST_TOML}\` lists
\`[[lineage]]\` entries, those are the templates this one was built on --
each with the repo URL and commit it was taken at, so you can go read any of
them at the exact state that was used. They are provenance, not something to
adapt here.
WELCOME_EOF

# --- 8.5 overwrite README.md to describe the template ---------------------

# The clean base's README describes the generic default-workspace-template.
# That is wrong for a published template: the repo's landing page -- the
# thing that decides whether a person boots this at all -- must sell THIS
# project. The structure below is the house recipe: hero graphic, the "Open in
# Minds" call-to-action, why you care, how to use it, ideas for making it
# yours. Deterministic full-file write, regenerated on every publish.
#
# The call-to-action points at the HTTPS trampoline rather than a bare
# minds:// URL, which GitHub renders dead. Both it and the copyable fallback
# need the repo URL, which does not exist yet (the repo name is confirmed in
# §6 and the owner comes back from the create call in §8), so both carry
# ${REPO_URL_PLACEHOLDER} for the lead to substitute before the push.

cat > README.md <<README_EOF
<p align="center">
  <img alt="${TITLE}" src="${THUMBNAIL}" width="480">
</p>

# ${TITLE}

<p align="center">
  <a href="https://boweiliu.github.io/open-in-minds/?git_url=https://github.com/${REPO_URL_PLACEHOLDER}"><img alt="Open in Minds" height="64" src="https://img.shields.io/badge/Open%20in%20Minds-D8D1C0?style=for-the-badge"></a>
</p>

Didn't work? Create a Minds workspace and paste this to your agent:
\` /use-template https://github.com/${REPO_URL_PLACEHOLDER}\`

## Why you care

${manifest_description}

<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment
with one or two plain sentences on the PROBLEM this solves -- why someone
would want it, not how it is built. Write for a human browsing GitHub who has
never seen the original mind. -->

## How to use it

<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment
with how someone actually USES this once it is running: the commands,
endpoints, screens, or workflow it exposes. This is the heart of the page, so
give it the room it needs -- but default to concise and readable; a short list
or a couple of worked examples beats a wall of prose. -->

## Ideas for making it yours

<!-- FILL-IN (publishing agent): BEFORE reporting done, replace this comment
with three to five CONCRETE changes someone could make after adopting this
(e.g. "point it at a different channel", "swap the daily digest for a weekly
one", "add a second source alongside Slack"). These are optional invitations
that show the thing is a starting point -- NOT the manifest's "Requirements",
which are the things that must be resolved. Do not repeat items across the
two. -->

## What this is

This repository is a published **minds template**: a clean, bootable
snapshot of what a mind built, ready to adapt into your own. It is NOT the
generic workspace template -- it is this specific project.

[\`${MANIFEST}\`](${MANIFEST}) is the full manifest -- what it is, how it
works, what it needs to run, and what to adapt -- with the
machine-readable half (recipe, requirements, and the environment it needs
installed) in [\`${MANIFEST_TOML}\`](${MANIFEST_TOML}).
README_EOF

# --- 8.6 remove the version history so it never ships in a template ------

# docs/VERSION_HISTORY.md is WORKSPACE-only, never part of a template: it records
# where a mind came from and every template it has published (slugs, repo
# URLs, source commits). None of that belongs in a published template -- and
# after an update-self, BASE_REF's tree can carry an accumulated copy of it --
# so drop it from the snapshot entirely. A mind created from this template
# grows its OWN ledger when it first runs update-self or publishes (update-self
# and publish-template write the starter on demand if the file is absent), so
# nothing is lost by omitting it here. `rm -f` is safe whether or not the base
# tree carried the file. This runs AFTER the no-diff guard so it can never make
# an empty include set look like it had something to publish.
rm -f docs/VERSION_HISTORY.md

# --- 9. boot smoke-check WITHOUT side effects, then single commit -------------

# Validate system/supervisord.conf via the supervisor python lib -- realize() +
# process_config() parse and check the config WITHOUT starting the daemon.
# NEVER `supervisord -t`: in supervisord, -t means --strip_ansi and LAUNCHES the
# daemon. If the lib is unavailable, skip the check (config holes in selected
# apps are acceptable; the base booting is what matters).
#
# Run the check with the interpreter that already ships the supervisor lib --
# the one behind the installed `supervisord` binary's shebang (system python) --
# NOT `uv run`. `uv run` would resolve and BUILD the whole project environment
# (workspace sources, native wheels like line-profiler) just to import one lib:
# many seconds on a cold clean base, and it can fail outright on an unrelated
# build error, spuriously aborting a publish that is otherwise fine. Deriving
# the interpreter from the supervisord shebang keeps the check ~0.1s and robust.
smoke_ok=1
if [ -f "system/supervisord.conf" ]; then
    SMOKE_PY="python3"
    SUPERVISORD_BIN="$(command -v supervisord 2>/dev/null || true)"
    if [ -n "$SUPERVISORD_BIN" ]; then
        shebang="$(head -1 "$SUPERVISORD_BIN" 2>/dev/null || true)"
        case "$shebang" in
            '#!'*)
                # First token after the "#!" is the interpreter path.
                candidate="$(printf '%s' "${shebang#\#!}" | awk '{print $1}')"
                [ -x "$candidate" ] && SMOKE_PY="$candidate"
                ;;
        esac
    fi
    if ! "$SMOKE_PY" - <<'PYEOF'
import sys

try:
    from supervisor.options import ServerOptions
except Exception:
    # supervisor lib unavailable in this interpreter -- skip the check.
    sys.exit(0)

options = ServerOptions()
options.configfile = "system/supervisord.conf"
options.realize(args=[])
options.process_config(do_usage=False)
PYEOF
    then
        smoke_ok=0
    fi
fi
if [ "$smoke_ok" -ne 1 ]; then
    echo "build_template.sh: boot smoke-check FAILED -- system/supervisord.conf did not realize cleanly" >&2
    exit 4
fi

# --- 9.5 validate the generated manifest -------------------------------------

# Schema + cross-file agreement, from the snapshotted validator and its schema
# module. The apt-resolution half is skipped HERE and only here: this run sees
# the freshly-generated skeleton, whose [environment] is still empty, so there
# is nothing to resolve yet. The worker re-runs this command WITHOUT
# --skip-apt-check once it has filled the declarations in (that is the run that
# rejects an unmirrorable package), and the lead runs it again before the push.
#
# `uv run --no-project` resolves no workspace project at all -- the same reason
# the smoke check above avoids plain `uv run`, and what makes this ~1s on a
# cold base rather than a full environment build that can fail on something
# unrelated.
if ! uv run --no-project --with 'pydantic>=2' python \
    "$SCAN_TOOLS_DIR/validate_template.py" "$REPO" \
    --skip-apt-check --allow-unfinished; then
    echo "build_template.sh: the generated ${MANIFEST_TOML} did not validate (see above)" >&2
    exit 6
fi

# --- 10. single commit, parented on BASE_REF (never on the mind's HEAD) ------

# The snapshot commit's parent is BASE_REF, NOT the branch's previous HEAD.
# This is a privacy invariant: the published repo's history must be the public
# template's history plus the snapshot commits -- never the mind's own commit
# history. Parenting on HEAD would ship every commit the mind ever made
# (including any secret that was ever committed and later removed: history
# keeps it retrievable), and would defeat published-version modifications
# ("publish a secret-cleaned copy of this file") entirely. commit-tree writes
# the already-validated assembled tree with the base as parent; reset --soft
# moves the branch there without touching the worktree or index.
git add -A
SNAPSHOT_COMMIT="$(git commit-tree "$(git write-tree)" -p "$BASE_REF" -m "template: ${SLUG}

Assembled on clean DEFAULT_WORKSPACE_TEMPLATE base ${BASE_REF} (provenance link only; no upstream fetch).")"
git reset --soft "$SNAPSHOT_COMMIT"

# --- 11. summary for the worker's done report --------------------------------

echo "build_template.sh: assembled template '${SLUG}' on clean base ${BASE_REF}"
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
echo "  manifest:  ${MANIFEST} (prose) + ${MANIFEST_TOML} (machine-readable)"
echo "  thumbnail: ${THUMBNAIL}"
echo "  readme:    README.md (regenerated to describe this template)"
echo "  boot smoke-check: passed"
echo "  manifest validation: passed (skeleton)"
echo "  NEXT, before reporting done:"
echo "    1. ${MANIFEST} has <!-- FILL-IN (publishing agent): ... --> blocks in 'What it is',"
echo "       'How it works', 'Requirements', 'Environment', and"
echo "       'Publication history' (the v1 entry). Replace ALL of them with real content, or"
echo "       explicit 'none' prose."
echo "    2. ${MANIFEST_TOML} has FILL-IN comments for the recipe's exclude and"
echo "       modification_rules, the [requirements] tables, and the [environment] table."
echo "       Every requires_ line you write in ${MANIFEST} MUST have its counterpart here --"
echo "       validation compares them and fails the publish if they disagree."
echo "    3. README.md has FILL-IN blocks for 'Why you care', 'How to use it', and 'Ideas for"
echo "       making it yours'."
echo "    4. ${THUMBNAIL} is a generic placeholder (marker comment inside). Replace the whole"
echo "       file with a bespoke SVG for this app; it is also the README's hero graphic."
echo "    5. Re-run the validator with NO --skip-apt-check and NO --allow-unfinished:"
echo "         uv run --no-project --with 'pydantic>=2' python \\"
echo "             .agents/skills/publish-template/scripts/validate_template.py ."
echo "       That run is the one that rejects an apt package which does not resolve in the"
echo "       pinned mirror, and any placeholder you left behind. Then commit."
