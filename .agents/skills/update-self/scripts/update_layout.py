"""Where the served workspace lives: the system interface, the vendored mngr and its
uv tool, the Python apps and their tools, the frontend bundle, and the provisioner
the apply re-runs.
"""

from __future__ import annotations

# The pinned-toolchain provisioner: the one script every provider runs to
# install the global toolchain (system packages, language runtimes, pinned
# CLIs). Its effects land at image-build / workspace-create time rather than at
# runtime, so a change to it -- or to any installer it chains -- never reaches
# a *live* workspace by restarting a service: the apply re-runs it live
# (idempotent) for the files it reads (:func:`read_provisioner_inputs`).
PROVISIONER_SCRIPT = "system/scripts/setup_system.sh"


# The served app, the editable tool the live service runs from, and the build
# surfaces. These mirror system/scripts/build_workspace.sh -- the source of
# truth for how the served environment is constructed.
SYSTEM_INTERFACE_DIR = "system/apps/system_interface"

FRONTEND_DIR = f"{SYSTEM_INTERFACE_DIR}/frontend"

# The vendored mngr the workspace runs on, and the uv tool built from it. An
# editable install pins the *source path*, not the dependency closure -- so the
# moment a merge advances this tree, the ``mngr`` CLI starts running new code
# against whatever was resolved for the old code.
MNGR_VENDOR_DIR = "system/vendor/mngr"

MNGR_DIR = f"{MNGR_VENDOR_DIR}/libs/mngr"

MNGR_TOOL_NAME = "imbue-mngr"

MNGR_EXECUTABLE = "mngr"

TOOL_NAME = "system-interface"

# uv records how a tool was installed here, inside the tool's own directory.
RECEIPT = "uv-receipt.toml"

# The plugin set each tool is built with, as build_workspace.sh reads it. The
# receipt only knows the plugins a tool was installed with *last time*, so a
# release that ships a new plugin needs this to reach an existing workspace:
# the merged tree's manifest is unioned into the reinstall, keyed by the name
# the plugin table uses for each tool -- ``mngr`` for the mngr tool, and an
# app's manifest name (``system/apps/<package>/app.toml``) for the app's tool.
PLUGIN_MANIFEST_PATH = "system/config/mngr_plugins.toml"

MNGR_PLUGIN_KEY = "mngr"

# Every ``system/apps/<package>/`` with both a ``pyproject.toml`` and an
# ``app.toml`` manifest is a Python app that runs from its own uv tool
# environment (see build_workspace.sh); the manifest names it and says whether
# it is critical (a snapshot-and-rollback target in the apply). An app with a
# pyproject but no manifest was scaffolded before manifests existed and still
# runs ``uv run <name>`` from the root venv until the migration rewrites it.
APPS_DIR = "system/apps"

MANIFEST_FILENAME = "app.toml"

# The frontend build output the backend serves at ``/``. Both ``node_modules``
# and this ``static/`` bundle are gitignored, so they never appear in a diff --
# they are protected by the pre-apply snapshots instead.
STATIC_DIR = f"{SYSTEM_INTERFACE_DIR}/imbue/system_interface/static"

FRONTEND_BUILD_INDEX = f"{STATIC_DIR}/index.html"

# The identity stamp the frontend build writes into the bundle: the git tree
# hash of the frontend source directory at the checkout's HEAD commit (an npm
# `postbuild` step in frontend/package.json running `git rev-parse HEAD:./`;
# best-effort, absent when the build ran with no git repo). It names the
# committed tree, not the working tree: uncommitted frontend edits at build
# time are not reflected in it, so a worker's bundle only describes its source
# when the worker built after committing. The apply compares it against the
# merged tree's own frontend tree hash, so a populated bundle built from some
# other source -- a wrong --worker-bundle path, an old worker's leftovers --
# falls back to a live build instead of being served as if it were the merged
# source. A live build in the merged checkout stamps that same hash, so for it
# the comparison is only a consistency check; the postbuild runs after any
# exit-0 build, and a build that wrote nothing is caught by the index check
# (vite empties the output directory first), not by the stamp.
BUNDLE_STAMP_FILENAME = ".source-tree-hash"

# The environment the provisioner runs under when re-run live: the image
# build's, not the calling agent's. Root's passwd home moves to /home/user at
# runtime, so an agent-driven run carries HOME=/home/user, and an installer
# that follows $HOME then lands beside neither the checks nor the PATH entries
# the script fixes to /root/.local (this is what a Claude pin bump hit). Only
# the two values that diverge are pinned; everything else ambient (proxies,
# apt configuration) is kept.
PROVISIONER_HOME = "/root"

PROVISIONER_PATH = (
    "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)

DEFAULT_WORKSPACE_URL = "http://127.0.0.1:8000"

ENV_WORKSPACE_URL = "MINDS_WORKSPACE_SERVER_URL"
