"""Files dropped on the viewer land in the Screen's desktop folder (docs/system/specs/screen/spec.md section 4.3),
the folder under the workspace's ``data/`` the service names in the session's ``user-dirs.dirs`` (``user_dirs.py``),
which the service then shows in a file manager window since the desktop's own icons are off. Each file keeps its
name; a clash takes a numeric suffix, settled by an exclusive create so two drops never write the same file."""

import os
import shutil
from pathlib import Path
from typing import Final
from typing import IO

from imbue.imbue_common.pure import pure
from screen.errors import FileDropError
from screen.primitives import DroppedFileName

# The cap on one drop, every file together (the route answers 413 past it): a few documents or pictures, never a
# disk image, since the body is held in memory or on the small tmpfs while it is parsed.
MAX_DROP_BYTES: Final[int] = 64 * 1024 * 1024

# How many suffixed names are tried for one dropped file before the drop is refused.
_MAX_CLASH_ATTEMPTS: Final[int] = 1000


@pure
def suffixed_file_name(name: DroppedFileName, attempt: int) -> str:
    """The name tried on the ``attempt``-th clash: the name itself first, then ``<stem>-<attempt><suffix>``."""
    if attempt == 0:
        return str(name)
    as_path = Path(str(name))
    return f"{as_path.stem}-{attempt}{as_path.suffix}"


def save_dropped_file(directory: Path, name: DroppedFileName, stream: IO[bytes]) -> Path:
    """Write ``stream`` into ``directory`` under ``name``, or the first free suffixed name when it is taken; the folder
    is created when missing. Raises ``FileDropError`` when the folder cannot be written."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise FileDropError(f"the desktop folder {directory} could not be created: {e}") from e
    for attempt in range(_MAX_CLASH_ATTEMPTS):
        target = directory / suffixed_file_name(name, attempt)
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        except OSError as e:
            raise FileDropError(f"{target} could not be created: {e}") from e
        try:
            with os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(stream, output)
        except OSError as e:
            # The exclusive create made the name this drop's own, so a half-written file is removed rather than
            # left on the desktop looking whole.
            target.unlink(missing_ok=True)
            raise FileDropError(f"{target} could not be written: {e}") from e
        return target
    raise FileDropError(f"{directory} already holds {_MAX_CLASH_ATTEMPTS} files named after {name}")
