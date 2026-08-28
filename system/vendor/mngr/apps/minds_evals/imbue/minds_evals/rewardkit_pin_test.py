"""The verifier container's rewardkit and this project's dev-group rewardkit are one pin.

``templates/`` runs inside the verifier container, which resolves rewardkit itself
through uvx: the criteria under ``templates/tests/`` and the ones under
``templates/outcome/`` (copied in beside them for cases that declare expectations)
both import it. The dev group installs it here too -- nothing this project runs
imports it, it is there so the type checker can resolve those criteria. Two separate
installs of the same thing, and only a matching pin makes checking the first tell you
anything about the second.

Where each part of this project is type-checked, and why, is recorded in
``[tool.ty.src]``; the root workspace's half of that split is enforced by
``test_standalone_project_ty_carve_outs_are_checked_by_the_root_workspace`` in
``test_meta_ratchets.py``, which runs on every PR.
"""

import re
import tomllib
from pathlib import Path

_PROJECT_DIR = Path(__file__).parent.parent.parent


def test_rewardkit_pin_matches_the_verifier_container() -> None:
    """Checking templates/ against a different rewardkit than the one that grades a
    trial reports a clean type check for code that cannot run."""
    dev_group = tomllib.loads((_PROJECT_DIR / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    declared = [str(entry) for entry in dev_group if str(entry).startswith("harbor-rewardkit")]
    assert len(declared) == 1, (
        f"expected exactly one harbor-rewardkit entry in the dev group, got {declared}; without it "
        "templates/ cannot be type-checked and its criteria fail to resolve `rewardkit`"
    )

    test_sh = (_PROJECT_DIR / "imbue" / "minds_evals" / "templates" / "tests" / "test.sh").read_text()
    match = re.search(r"uvx --from '(harbor-rewardkit[^']*)'", test_sh)
    assert match is not None, "could not find the uvx rewardkit invocation in templates/tests/test.sh"

    assert declared[0] == match.group(1), (
        f"dev group pins {declared[0]!r} but templates/tests/test.sh runs {match.group(1)!r}; "
        "the type check would not be against the version that grades trials"
    )
