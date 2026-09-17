#!/usr/bin/env bash
# Generate the .dockerignore used to build docker/offload sandbox images.
#
# The file is .gitignore minus the entries that must ship in images
# (.dockerignore's own entry, and the generated Dockerfile.release), plus
# docker-form re-include negations for committed paths that a broad exclude
# would otherwise swallow. Two things make those necessary: the docker-style
# matcher offload/Modal apply to .dockerignore does not honor .gitignore's
# anchored `!/...` negation form, and it never sees a nested .gitignore at
# all, since only the repo-root file is read here. So a committed path
# re-included either way needs a docker-form line appended below or it
# silently vanishes from sandbox images:
#
#   - .minds/template/*.sh, the committed schemas, re-included in the root
#     .gitignore with the anchored form. The .minds/<env>/ operator secrets
#     stay excluded either way.
#   - the vendored viewer's app/lib/, real frontend source caught by the
#     repo-wide `**/lib/` exclude for Python build output and re-included in
#     apps/minds_evals/.gitignore.
#
# test_meta_ratchets.py::test_generated_dockerignore_ships_all_committed_files
# runs this script against Modal's real matcher and fails on any committed
# file that would be dropped, so a new re-include is caught there.
#
# This script is listed in the offload configs' [checkpoint] build_inputs:
# the sandbox base image's `COPY . /code/mngr/` is filtered by the file this
# generates, so any change to the generation logic must rebuild the
# checkpoint image (a change routed only through .gitignore rebuilds too,
# since .gitignore is a build input alongside this script).
#
# Usage: generate_dockerignore.sh [output-path]
# The repo-root .gitignore is always the source, regardless of cwd. The
# output defaults to the repo-root .dockerignore; an explicit output-path is
# honored as given (resolved against the caller's cwd if relative).
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$repo_root/.dockerignore}"
grep -vE '^/?\.dockerignore$|^/libs/mngr/imbue/mngr/resources/Dockerfile\.release$' "$repo_root/.gitignore" > "$out"
printf '!.minds/template\n!.minds/template/**\n' >> "$out"
printf '!apps/minds_evals/viewer/app/lib\n!apps/minds_evals/viewer/app/lib/**\n' >> "$out"
