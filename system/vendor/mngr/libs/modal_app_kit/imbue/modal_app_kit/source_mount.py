"""Rules for which local Python source ships into our Modal containers.

Our Modal apps are deployed by file path (``modal deploy .../app.py``), so the
entrypoint file itself is auto-mounted to ``/root/app.py``; everything else the
app needs is shipped with a SINGLE ``Image.add_local_python_source(...)`` call
as the last image operation. The predicate built here decides which files of
those packages ship. See libs/modal_app_kit/README.md for why the deploy works
this way.
"""

from pathlib import Path

import modal

# Only .py files ship (mirrors modal's own NON_PYTHON_FILES default): bytecode,
# data files, and editor droppings can never churn the upload set.
_NON_PYTHON_FILES = ~modal.FilePatternMatcher("**/*.py")

# Test files never ship. The bare (unanchored-directory) "app.py" pattern
# matches only the package-root entrypoint: it already ships via Modal's
# automatic entrypoint file mount as /root/app.py (imported as top-level
# module `app`), and shipping a second copy inside the package would let an
# accidental `import <package>.app` execute the module twice under a
# different name. Excluding it makes such an import fail loudly instead.
_NON_SHIPPED_PYTHON_FILES = modal.FilePatternMatcher(
    "**/*_test.py",
    "**/test_*.py",
    "**/conftest.py",
    "**/testing.py",
    "app.py",
)


def shipped_python_source_ignore(path: Path) -> bool:
    """The ``ignore`` predicate for ``Image.add_local_python_source``.

    Receives paths relative to each mounted package root; returns True for
    files that must NOT ship.
    """
    return bool(_NON_PYTHON_FILES(path)) or bool(_NON_SHIPPED_PYTHON_FILES(path))
