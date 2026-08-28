"""Project-level conftest for minds_admin.

When running tests from apps/minds_admin/, this conftest provides the common
pytest hooks that would otherwise come from the monorepo root conftest.py
(which is not discovered when pytest runs from a subdirectory). When running
from the monorepo root, the root conftest.py registers the hooks first and
this call is a no-op (guarded by a module-level flag).

Also registers mngr's shared plugin test fixtures, including the autouse
setup_test_mngr_env that redirects HOME to a temp dir so tests cannot read or
write the real ~/.mngr, ~/.minds*, or ~/.claude.json.
"""

from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_conftest_hooks(globals())

register_plugin_test_fixtures(globals())
