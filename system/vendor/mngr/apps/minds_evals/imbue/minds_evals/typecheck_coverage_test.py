"""Where each part of this project gets type-checked, and why it is split.

This project is not a uv workspace member, so its modules are checked against its
own venv by ``just test-minds-evals``. ``resources/`` cannot be: those files are
shipped as source into the eval box and run against the *monorepo* venv, importing
packages this project deliberately does not depend on. The root workspace checks
them instead, which is also the environment they actually execute in.

That split is only correct while both halves hold, and nothing else in either
project fails if they stop holding -- the files simply stop being checked anywhere.

``templates/`` is the other half of the same question and resolves the other way:
its criteria import ``rewardkit``, which the verifier container installs via uvx,
but the dev group installs it here too, so this project can and does check them.
"""

import re
import tomllib
from pathlib import Path

_PROJECT_DIR = Path(__file__).parent.parent.parent


def test_box_resources_are_type_checked_by_the_root_workspace() -> None:
    """resources/ must be excluded here and NOT excluded by the root workspace.

    These files are shipped as source into the eval box and run there against the
    monorepo venv, importing packages this project deliberately does not depend on
    (mngr_forward, litellm). So this project cannot check them and the root
    workspace must -- it is the environment they actually execute in. Both halves
    are load-bearing: exclude them in both places and they are type-checked nowhere
    at all, while every test in both projects still passes.

    The root side is checked for *directory* excludes that swallow resources/,
    which is the whole realistic regression: replacing the root's per-module
    entries with a plain ``apps/minds_evals/``. File globs such as ``*.py`` are
    deliberately not interpreted -- emulating ty's matcher here would be its own
    source of bugs, and a file glob cannot hide a directory.
    """
    resources_dir = _PROJECT_DIR / "imbue" / "minds_evals" / "resources"
    assert resources_dir.is_dir(), f"{resources_dir} is missing; update this check with it"

    own_exclude = tomllib.loads((_PROJECT_DIR / "pyproject.toml").read_text())["tool"]["ty"]["src"]["exclude"]
    assert "imbue/minds_evals/resources/" in own_exclude, (
        "this project's [tool.ty.src] exclude must keep resources/, whose imports it cannot resolve"
    )

    repo_root = _PROJECT_DIR.parent.parent
    root_exclude = tomllib.loads((repo_root / "pyproject.toml").read_text())["tool"]["ty"]["src"]["exclude"]
    resources_relative = resources_dir.relative_to(repo_root).as_posix()
    swallowing = [
        pattern
        for pattern in root_exclude
        if "*" not in pattern and f"{resources_relative}/".startswith(pattern.rstrip("/") + "/")
    ]
    assert not swallowing, (
        f"the root [tool.ty.src] exclude hides {resources_relative}/ via {swallowing}, and this "
        "project excludes it too, so those files would be type-checked nowhere"
    )


def test_verifier_templates_are_type_checked_by_this_project() -> None:
    """templates/ must NOT be excluded here, and rewardkit must stay installable.

    Nothing this project runs imports rewardkit -- it is a dev dependency purely so
    the type checker can resolve templates/tests/, whose criteria import it. That
    makes it the kind of dependency someone prunes as unused, which would silently
    take the templates with it: excluding them costs nothing visible, because they
    execute only inside the verifier container.
    """
    templates_dir = _PROJECT_DIR / "imbue" / "minds_evals" / "templates"
    assert templates_dir.is_dir(), f"{templates_dir} is missing; update this check with it"

    pyproject = tomllib.loads((_PROJECT_DIR / "pyproject.toml").read_text())
    excluded = pyproject["tool"]["ty"]["src"]["exclude"]
    swallowing = [pattern for pattern in excluded if "templates" in pattern]
    assert not swallowing, (
        f"[tool.ty.src] exclude hides templates/ via {swallowing}; rewardkit is a dev dependency "
        "precisely so those files can be checked here"
    )

    dev_group = pyproject["dependency-groups"]["dev"]
    assert any(str(entry).startswith("harbor-rewardkit") for entry in dev_group), (
        "harbor-rewardkit must stay in the dev group; without it templates/tests/ cannot be "
        "type-checked and its criteria fail to resolve `rewardkit`"
    )


def test_rewardkit_pin_matches_the_verifier_container() -> None:
    """The dev-group rewardkit pin must equal the one test.sh runs in the container.

    The container resolves rewardkit itself through uvx, so these two are separate
    installs of the same thing. Checking templates/ against a different major line
    than the one that actually grades a trial would report a clean type check for
    code that cannot run.
    """
    dev_group = tomllib.loads((_PROJECT_DIR / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    declared = [str(entry) for entry in dev_group if str(entry).startswith("harbor-rewardkit")]
    assert len(declared) == 1, f"expected exactly one harbor-rewardkit entry in the dev group, got {declared}"

    test_sh = (_PROJECT_DIR / "imbue" / "minds_evals" / "templates" / "tests" / "test.sh").read_text()
    match = re.search(r"uvx --from '(harbor-rewardkit[^']*)'", test_sh)
    assert match is not None, "could not find the uvx rewardkit invocation in templates/tests/test.sh"

    assert declared[0] == match.group(1), (
        f"dev group pins {declared[0]!r} but templates/tests/test.sh runs {match.group(1)!r}; "
        "the type check would not be against the version that grades trials"
    )
