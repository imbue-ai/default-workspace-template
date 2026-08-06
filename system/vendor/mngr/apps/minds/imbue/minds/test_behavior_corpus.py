"""Guard the live behavior corpus shipped at ``apps/minds/behaviors/``.

This corpus is a minds artifact: it travels with minds (and any future
spin-out), so this guard lives in the minds app rather than in the
corpus-generic ``mngr_behaviors`` tool. It fails if the corpus ever drifts out of
conformance with the behavior language that ``mngr behaviors validate``
enforces.
"""

from pathlib import Path

from imbue.mngr_behaviors.corpus import scan_corpus

# The live corpus shipped in this repo (this test sits at
# apps/minds/imbue/minds/, so parents[2] is apps/minds).
_LIVE_CORPUS_ROOT = Path(__file__).resolve().parents[2] / "behaviors"


def test_live_corpus_has_no_violations() -> None:
    """The corpus at ``apps/minds/behaviors/`` always satisfies the behavior-language rules."""
    scan = scan_corpus(_LIVE_CORPUS_ROOT)

    assert scan.violations == ()
    # Guard against the root silently pointing at an empty or wrong directory.
    assert len(scan.units) > 0
