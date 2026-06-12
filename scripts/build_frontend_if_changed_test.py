"""Tests for the build_frontend_if_changed.py reveal-step helper.

Covers the pure decision logic -- the inputs hash and the rebuild predicate --
without invoking ``npm`` (the build itself is verified by running the script
against the real frontend).
"""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent / "build_frontend_if_changed.py"
_spec = importlib.util.spec_from_file_location("build_frontend_if_changed", _SCRIPT)
assert _spec is not None and _spec.loader is not None
build_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_mod)


def _make_frontend(root: Path) -> Path:
    """Create a minimal frontend tree with one source file and a config file."""
    frontend = root / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "index.ts").write_text("console.log('hi');\n")
    (frontend / "index.html").write_text("<html></html>\n")
    (frontend / "package.json").write_text("{}\n")
    return frontend


def test_inputs_hash_changes_when_a_source_file_changes(tmp_path: Path) -> None:
    frontend = _make_frontend(tmp_path)
    before = build_mod.compute_inputs_hash(frontend)
    (frontend / "src" / "index.ts").write_text("console.log('changed');\n")
    after = build_mod.compute_inputs_hash(frontend)
    assert before != after


def test_inputs_hash_changes_when_a_new_source_file_is_added(tmp_path: Path) -> None:
    frontend = _make_frontend(tmp_path)
    before = build_mod.compute_inputs_hash(frontend)
    (frontend / "src" / "extra.ts").write_text("export const x = 1;\n")
    after = build_mod.compute_inputs_hash(frontend)
    assert before != after


def test_inputs_hash_ignores_files_outside_the_tracked_inputs(tmp_path: Path) -> None:
    frontend = _make_frontend(tmp_path)
    before = build_mod.compute_inputs_hash(frontend)
    # eslint config and the built output are not build inputs.
    (frontend / "eslint.config.js").write_text("export default [];\n")
    after = build_mod.compute_inputs_hash(frontend)
    assert before == after


def test_needs_build_true_when_bundle_missing(tmp_path: Path) -> None:
    frontend = _make_frontend(tmp_path)
    static = tmp_path / "static"
    hash_file = static / ".build_inputs_hash"
    needed, current = build_mod.needs_build(frontend, static, hash_file)
    assert needed is True
    assert current == build_mod.compute_inputs_hash(frontend)


def test_needs_build_true_when_hash_file_missing(tmp_path: Path) -> None:
    frontend = _make_frontend(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>\n")
    hash_file = static / ".build_inputs_hash"
    needed, _ = build_mod.needs_build(frontend, static, hash_file)
    assert needed is True


def test_needs_build_false_when_hash_matches(tmp_path: Path) -> None:
    frontend = _make_frontend(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>\n")
    hash_file = static / ".build_inputs_hash"
    hash_file.write_text(build_mod.compute_inputs_hash(frontend))
    needed, _ = build_mod.needs_build(frontend, static, hash_file)
    assert needed is False


def test_needs_build_true_when_hash_stale(tmp_path: Path) -> None:
    frontend = _make_frontend(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>\n")
    hash_file = static / ".build_inputs_hash"
    hash_file.write_text("deadbeef")
    needed, _ = build_mod.needs_build(frontend, static, hash_file)
    assert needed is True
