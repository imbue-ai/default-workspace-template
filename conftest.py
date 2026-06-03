"""Root conftest.

Its presence makes pytest (in the default ``prepend`` import mode) insert the
repo root onto ``sys.path`` for the whole session, so tests collected from
nested directories (e.g. ``.agents/skills/launch-task/scripts/``,
``libs/.../``) can ``import mngr_cli_contract`` -- the shared mngr-CLI argv
contract validator -- without per-test path manipulation.
"""

from __future__ import annotations
