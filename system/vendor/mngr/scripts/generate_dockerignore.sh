#!/usr/bin/env bash
# Generate the .dockerignore used to build docker/offload sandbox images.
#
# The file is .gitignore minus the entries that must ship in images
# (.dockerignore's own entry, and the generated Dockerfile.release), plus
# docker-form re-include negations for the committed .minds/template/*.sh
# schemas: .gitignore re-includes them with anchored `!/...` negations, but
# the docker-style matcher offload/Modal apply to .dockerignore does not
# honor that anchored form, so without the appended lines the committed
# schemas silently vanish from sandbox images. The .minds/<env>/ operator
# secrets stay excluded either way.
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
