#!/usr/bin/env bash
# SessionStart hook (codex): port of claude_update_plugin.sh -- installs/
# refreshes the imbue-code-guardian-codex plugin every session. Both
# `codex plugin marketplace add` and `codex plugin add` are confirmed
# idempotent (live-tested: exit 0 whether the marketplace/plugin is already
# present or not), so this can just unconditionally re-run them.
#
# TEMPORARY: points at a personal fork + PR branch
# (https://github.com/imbue-ai/code-guardian/pull/25), not the canonical
# imbue-ai/code-guardian repo, because the codex/antigravity/opencode
# plugin variants only exist there so far. Real, live-tested install path
# (codex plugin marketplace add owner/repo --ref branch -- confirmed real
# syntax via developers.openai.com/codex/plugins/build), not a guess.
# SWITCH BACK to `imbue-ai/code-guardian` (no --ref needed) once that PR
# merges -- this comment and the URL below are the marker for that follow-up.
set -euo pipefail

cat > /dev/null 2>&1 || true

codex plugin marketplace add minhtrinh-imbue/code-guardian --ref add-codex-opencode-antigravity-support >&2 || true
codex plugin add imbue-code-guardian-codex@imbue-code-guardian >&2 || true

exit 0
