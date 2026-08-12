#!/usr/bin/env bash
# Create an independent default-workspace-template checkout nested in the
# current mngr checkout at .external_worktrees/default-workspace-template. Errors if
# one is already there (remove it to recreate) or if the branch already exists on default_workspace_template.
#
#   just default-workspace-template-worktree                 # default_workspace_template on the SAME branch as this mngr checkout
#   just default-workspace-template-worktree wz/other        # override branch
#   just default-workspace-template-worktree wz/other main   # override base ref (default origin/main)
#
# The checkout is a full, independent clone (its own .git), so it survives deletion
# of any other clone or cache -- unlike a `git worktree` hung off a base repo, which
# breaks the moment that base is removed. Destination is derived from the current
# checkout's root, so it lands correctly whether run from an operator clone or an
# agent's ~/.mngr/worktrees/<agent>/ checkout, with no path baked in.
#
# DEFAULT_WORKSPACE_TEMPLATE_DIR (from a gitignored apps/minds/.env or the shell) is an OPTIONAL speed hint:
# a local default_workspace_template clone whose objects are borrowed via `git clone --reference-if-able`.
# `--dissociate` copies them in, so the result keeps no dependency on DEFAULT_WORKSPACE_TEMPLATE_DIR.
set -ueo pipefail

DEFAULT_WORKSPACE_TEMPLATE_REMOTE="https://github.com/imbue-ai/default-workspace-template.git"

repo_root="$(git rev-parse --show-toplevel)"
dest="$repo_root/.external_worktrees/default-workspace-template"
base="${2:-origin/main}"

# Branch for the new checkout. Defaults to the current branch of this mngr
# checkout via scripts/current_branch.sh, which is robust to the detached git
# HEAD that jj colocated repos normally sit in. Pass the branch as arg 1 to
# skip detection.
branch="${1:-}"
if [ -z "$branch" ]; then
    branch="$(bash "$repo_root/scripts/current_branch.sh" "$repo_root" || true)"
fi
if [ -z "$branch" ]; then
    echo "error: could not determine the current branch of $repo_root" >&2
    echo "       (git HEAD is detached and no jj bookmark points at @)." >&2
    echo "       pass the branch explicitly:  just default-workspace-template-worktree <branch>" >&2
    exit 1
fi

# Reject rather than silently reuse a stale checkout from an earlier task.
if [ -e "$dest" ]; then
    echo "error: $dest already exists" >&2
    if [ -e "$dest/.git" ]; then
        echo "       (default_workspace_template checkout on branch $(git -C "$dest" branch --show-current))" >&2
    fi
    echo "       remove it to recreate:  rm -rf $dest" >&2
    exit 1
fi

# Optional speed hint: borrow objects from a local default_workspace_template clone if one is configured.
if [ -z "${DEFAULT_WORKSPACE_TEMPLATE_DIR:-}" ] && [ -f "$repo_root/apps/minds/.env" ]; then
    set -a; . "$repo_root/apps/minds/.env"; set +a
fi
ref_args=()
if [ -n "${DEFAULT_WORKSPACE_TEMPLATE_DIR:-}" ] && git -C "$DEFAULT_WORKSPACE_TEMPLATE_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    ref_args=(--reference-if-able "$DEFAULT_WORKSPACE_TEMPLATE_DIR" --dissociate)
fi

# GH_TOKEN (agents have it) authenticates the private clone; operators without it
# fall back to git's configured credential helper. The token is never persisted.
url="$DEFAULT_WORKSPACE_TEMPLATE_REMOTE"
if [ -n "${GH_TOKEN:-}" ]; then
    url="https://x-access-token:${GH_TOKEN}@github.com/imbue-ai/default-workspace-template.git"
fi

# Reject a name that already exists on default_workspace_template rather than silently tracking that old
# branch -- a reused name (e.g. a year later) would otherwise inherit stale history
# instead of forking off $base. Checked before the clone so no work is wasted.
if git ls-remote --exit-code --heads "$url" "$branch" >/dev/null 2>&1; then
    echo "error: branch '$branch' already exists on default_workspace_template" >&2
    echo "       delete it:  git push $DEFAULT_WORKSPACE_TEMPLATE_REMOTE --delete $branch" >&2
    echo "       or pick a new name:  just default-workspace-template-worktree <name>" >&2
    exit 1
fi

mkdir -p "$repo_root/.external_worktrees"
git clone ${ref_args[@]+"${ref_args[@]}"} "$url" "$dest"
git -C "$dest" remote set-url origin "$DEFAULT_WORKSPACE_TEMPLATE_REMOTE"
git -C "$dest" checkout -q -b "$branch" "$base"

# Turn the code-guardian gates back on for this checkout. The committed
# .reviewer/settings.json carries the *workspace* policy -- every gate off, no
# auto-merge or push -- because that repo is also cloned into containers, where
# a workspace is the user's own isolated unit. A checkout nested here is the
# opposite case: active development of the template, which mngr's root
# .reviewer/settings.json declares as a second reviewed repo, and which should
# be gated exactly like mngr itself. settings.local.json is gitignored there
# and wins per-key, so the workspace policy stays intact upstream, and a
# container can never inherit these values (creation clones the branch, and
# this file is never committed).
#
# system/vendor/mngr is exempted from the clean-tree gate: `just minds-start`
# (and `sync-vendor-mngr-rsync` / `propagate-changes`) rsync the live mngr tree
# into that tracked subtree, deliberately leaving thousands of uncommitted
# files behind, and that machine-generated state is byte-identical to a
# deliberate revendor -- no gate can tell them apart. Review of that content
# is owned by the mngr repo (whatever legitimately lands there came from an
# mngr PR with its own gates), and a DWT PR missing a revendor it depends on
# fails its own CI. Everything else in the checkout keeps the clean-tree gate.
mkdir -p "$dest/.reviewer"
cat > "$dest/.reviewer/settings.local.json" <<'REVIEWER_SETTINGS'
{
    "stop_hook": {
        "base_branch": "main",
        "fetch_and_merge": true,
        "uncommitted_exempt_paths": ["system/vendor/mngr"]
    },
    "autofix": {
        "is_enabled": true
    },
    "verify_architecture": {
        "is_enabled": true
    },
    "ci": {
        "is_enabled": true
    }
}
REVIEWER_SETTINGS

echo "default_workspace_template checkout ready: $dest on $branch"
