#!/bin/bash
# Move this step's uploads out of the box's working directory.
#
# Harbor merges a step's workdir/ into the box's working directory -- which is the mngr checkout the
# box works from -- and then runs this script from there, before the step's agent starts. The
# uploads are relocated and this script deletes itself, so that checkout stays exactly what the
# image shipped: the workspace's vendored mngr is rsynced out of it, and anything left behind would
# travel into every workspace the trial creates.
#
# Generated from a template that substitutes named placeholders, so a dollar sign the shell must
# see is written twice in the template source.
set -euo pipefail

cd -- "$$(dirname -- "$$0")"
rm -rf "$destination"
mkdir -p "$destination_parent"
mv "$staged_dirname" "$destination"
rm -f setup.sh
