import os
from collections.abc import Mapping
from pathlib import Path

from imbue.mngr_behaviors.corpus import REQUIRED_README_INCIPIT


def write_behavior_corpus(
    corpus_root: Path,
    content_by_relative_path: Mapping[str, str],
    *,
    fill_readmes: bool = True,
) -> Path:
    """Materialize a synthetic behavior corpus for tests and return its root.

    By default every folder the caller did not give its own README.md is given
    one carrying the mandated incipit, so a corpus built for a test satisfies the
    folder-README mandate without each test restating it. Pass
    ``fill_readmes=False`` to exercise the missing-README rule itself.
    """
    for relative_path, content in content_by_relative_path.items():
        target = corpus_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    corpus_root.mkdir(parents=True, exist_ok=True)
    if fill_readmes:
        for folder_name, child_folder_names, file_names in os.walk(corpus_root):
            # Mirror the scanner: hidden entries are not corpus folders.
            child_folder_names[:] = [name for name in child_folder_names if not name.startswith(".")]
            if "README.md" not in file_names:
                (Path(folder_name) / "README.md").write_text(REQUIRED_README_INCIPIT + "\n", encoding="utf-8")
    return corpus_root
