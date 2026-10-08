import os
import subprocess
from pathlib import Path

from imbue.chat.background_tasks import BACKGROUND_TASKS_DIRNAME
from imbue.chat.background_tasks import BackgroundTask
from imbue.chat.background_tasks import load_background_task_reader
from imbue.chat.config import DEFAULT_CHAT_DATA_DIR
from imbue.chat.primitives import ChatId
from imbue.chat.testing import build_background_task_reader
from imbue.chat.testing import write_background_task_marker


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def test_the_reader_reads_the_live_markers_the_script_writes(tmp_path: Path) -> None:
    """The script loads by path from outside this package, and what it reads arrives in the app's types: live
    markers oldest first, a stale one (its pid dead) and a malformed one skipped."""
    reader = build_background_task_reader(tmp_path / "background_tasks")
    chat_id = ChatId("agent-1")
    write_background_task_marker(
        reader, chat_id, "later", os.getpid(), "Migrate the schema", "2026-10-08T12:05:00+00:00"
    )
    write_background_task_marker(
        reader, chat_id, "earlier", os.getpid(), "Rebuild the image", "2026-10-08T12:00:00+00:00"
    )
    write_background_task_marker(reader, chat_id, "stale", _dead_pid())
    (reader.chat_dir(chat_id) / "run_in_background-broken.json").write_text("{not json")

    assert reader.live_tasks(chat_id) == (
        BackgroundTask(
            source="run_in_background",
            id="earlier",
            description="Rebuild the image",
            started_at="2026-10-08T12:00:00+00:00",
            pid=os.getpid(),
        ),
        BackgroundTask(
            source="run_in_background",
            id="later",
            description="Migrate the schema",
            started_at="2026-10-08T12:05:00+00:00",
            pid=os.getpid(),
        ),
    )
    assert reader.live_tasks(ChatId("agent-without-markers")) == ()


def test_the_chat_app_reads_the_marker_root_the_script_writes_by_default() -> None:
    """The writers fall back to the script's default root when ``MINDS_BACKGROUND_TASKS_DIR`` is unset; the chat app
    reads its own data dir's ``background_tasks``. Both are repo-root relative and must name one directory."""
    reader = build_background_task_reader(DEFAULT_CHAT_DATA_DIR / BACKGROUND_TASKS_DIRNAME)
    assert DEFAULT_CHAT_DATA_DIR / BACKGROUND_TASKS_DIRNAME == reader.script.DEFAULT_MARKER_ROOT


def test_a_boot_with_no_script_reads_no_tasks(tmp_path: Path) -> None:
    """The update's pre-flight boots in a directory with no workspace beneath it; that must not stop the app."""
    assert load_background_task_reader(tmp_path / "background_tasks", tmp_path / "missing.py") is None
