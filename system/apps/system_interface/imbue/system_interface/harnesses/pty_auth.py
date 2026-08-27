"""Harness-agnostic machinery for driving a CLI's sign-in through a PTY.

Every harness signs in the same shape: spawn the CLI on a pseudo-terminal, wait
for a pattern, send some keystrokes, scrape a value the user needs (a URL or a
one-time code) off the rendered screen, and hand a value back. Only the argv,
the keystrokes and the patterns differ.

This module owns the parts that do not differ. It was extracted verbatim from
``claude/auth.py``, which is still its only caller -- the per-harness
descriptors that will make it multi-consumer arrive with the other lanes. The
extraction is deliberately behaviour-preserving: nothing here decides *what* to
scrape, only *how* to recover it from a stream that a terminal renderer has
already mangled.

Two things make that recovery non-obvious, and both are why this is a module
rather than a few inline calls:

* A CLI's renderer emits diff-based frames full of cursor positioning, so the
  raw byte order does not correspond to the visual layout. Only replaying the
  stream through a terminal emulator at the *exact* PTY geometry recovers what
  was on screen -- which is why the spawn geometry and the replay geometry are
  the same two numbers, passed together.
* A value longer than the terminal is width-wrapped across rows, so it has to
  be de-wrapped. An OSC 8 hyperlink target, when the CLI emits one, carries the
  value unwrapped and is always preferred.

Harness-specific knobs are parameters with the claude-shaped default, so the
existing caller is unchanged and a harness that differs can say so:
``frame_marker`` (Ink's synchronized-update marker -- agy emits none, in which
case the replay collapses to a single final-screen snapshot) and the PTY
geometry.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any
from typing import Final

import pexpect
import pyte
from loguru import logger as _loguru_logger

from imbue.imbue_common.pure import pure

logger = _loguru_logger


class PtyAuthError(RuntimeError):
    """A PTY-driven sign-in flow could not be completed.

    Harness-specific auth errors subclass this so a caller can catch the whole
    family; ``claude/auth.py``'s ``ClaudeAuthError`` is one such subclass, which
    is what keeps its endpoint handlers catching a single type.
    """


# An OSC 8 terminal hyperlink: `ESC ] 8 ; params ; target (BEL | ESC \)`.
# The params field is not always empty (CLIs emit `id=...`). The target carries
# the full value with no width-wrapping, so it survives narrow PTYs that
# hard-wrap the visible label.
OSC8_HYPERLINK_REGEX: Final = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]+)(?:\x07|\x1b\\)")

# End-of-frame marker for Ink's synchronized-update rendering. Claude Code's CLI
# emits one after every frame; the replay in `extract_wrapped_value` snapshots
# the screen at each. A CLI that emits none is not broken -- the replay simply
# degrades to "the final screen only", which loses the longest-wins protection
# against a truncated mid-frame candidate.
INK_FRAME_END_MARKER: Final = "\x1b[?2026l"

# Pinned PTY geometry. These are pexpect's own defaults, made explicit because
# the spawn and the extraction replay MUST agree: the reconstructed screen's
# wrapping only corresponds to what the CLI rendered if both use the same size.
DEFAULT_PTY_LINES: Final = 24
DEFAULT_PTY_COLUMNS: Final = 80

# After a trigger regex fires, keep draining the PTY until the caller's
# completion predicate is satisfied or EOF; the deadline is only a hang
# backstop (generous: a token path drains to process exit).
DEFAULT_DRAIN_DEADLINE_SECONDS: Final = 15.0
DEFAULT_DRAIN_READ_SECONDS: Final = 0.25


def spawn_pty(
    executable: str,
    args: list[str],
    timeout: float,
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    lines: int = DEFAULT_PTY_LINES,
    columns: int = DEFAULT_PTY_COLUMNS,
) -> Any:
    """Spawn a CLI on a PTY at the geometry the extraction replays at.

    ``timeout`` becomes the spawn's default for a subsequent ``expect`` that
    does not pass its own -- the existing caller relies on exactly that, so the
    two are deliberately not split.

    ``env`` REPLACES the child's environment rather than merging into it (that
    is ``pexpect.spawn``'s behaviour, not a choice made here). A caller scoping
    a sign-in to one account's config dir must therefore pass a full
    environment -- ``{**os.environ, **scoped}`` -- or the child loses ``PATH``
    and never starts. ``TERM`` in particular is load-bearing: without it the
    CLI may not emit the escape sequences the replay depends on.
    """
    return pexpect.spawn(
        executable,
        args,
        timeout=timeout,
        encoding="utf-8",
        dimensions=(lines, columns),
        env=dict(env) if env is not None else None,
        cwd=cwd,
    )


def safe_terminate(process: Any) -> None:
    """Terminate a pexpect spawn without letting teardown errors propagate.

    `pexpect.spawn.isalive()` reaps the child's exit status and wraps
    `ptyprocess` errors in `pexpect.ExceptionPexpect`; `terminate()` can
    raise `OSError` on an already-reaped descriptor. Both live inside the
    try so a half-torn-down process never crashes the caller (called from
    every teardown path, including the auth-success chokepoint).
    """
    try:
        if not process.isalive():
            return
        process.terminate(force=True)
    except (OSError, pexpect.ExceptionPexpect) as e:
        logger.warning("auth subprocess terminate raised: {}", e)


def safe_close(process: Any) -> None:
    """Release the pexpect spawn's PTY file descriptor.

    `pexpect.spawn.close()` can raise `OSError` (e.g. on an already-closed
    descriptor) and `pexpect.ExceptionPexpect` in some teardown paths.
    Swallow + log both since the only thing we can do at this point is
    drop the reference anyway.
    """
    try:
        process.close()
    except (OSError, pexpect.ExceptionPexpect) as e:
        logger.warning("auth subprocess close raised: {}", e)


@pure
def extract_value_from_screen_rows(
    rows: list[str],
    start_regex: re.Pattern[str],
    continuation_regex: re.Pattern[str],
) -> tuple[str, bool] | None:
    """Find `start_regex` on one rendered screen, de-wrapping across rows.

    A value hard-wrapped by the renderer occupies its row through the last
    column and continues on the next row; a row with trailing blank space
    is the value's final row. Rows arrive space-padded to the full screen
    width (pyte's display invariant), which the wrap detection relies on.

    Returns the value plus whether it provably *ended*: a full-width row
    with only blank space under it is ambiguous (the continuation may not
    have been drawn yet on this frame), so only non-continuation content
    under the row proves the value ended at the screen edge.
    """
    for idx, row in enumerate(rows):
        match = start_regex.search(row)
        if match is None:
            continue
        value = row[match.start() :].rstrip()
        row_idx = idx
        # A row whose last column is occupied wrapped onto the next row.
        while rows[row_idx].rstrip() and len(rows[row_idx].rstrip()) == len(rows[row_idx]):
            candidate = rows[row_idx + 1].strip() if row_idx + 1 < len(rows) else ""
            if candidate == "":
                return value, False
            if continuation_regex.match(candidate) is None:
                return value, True
            value += candidate
            row_idx += 1
        return value, True
    return None


@pure
def extract_wrapped_value(
    raw_output: str,
    start_regex: re.Pattern[str],
    continuation_regex: re.Pattern[str],
    *,
    frame_marker: str | None = INK_FRAME_END_MARKER,
    lines: int = DEFAULT_PTY_LINES,
    columns: int = DEFAULT_PTY_COLUMNS,
) -> str | None:
    """Recover a possibly width-wrapped value from a raw PTY stream.

    The CLI's renderer emits diff-based frames full of cursor positioning, so
    the raw stream's byte order does not correspond to the visual layout --
    only a terminal-emulator replay at the exact PTY geometry recovers what was
    actually on screen. The stream is replayed frame by frame (split on the
    synchronized-update end marker the renderer emits after each frame) and the
    longest provably-terminated candidate across ALL frames wins: a single
    mid-frame screen can show a truncated prefix over the previous frame's
    stale content, and the final screen alone can miss the value entirely if
    the CLI clears it on exit. A truncated candidate is a strict prefix of the
    real one, so longest-wins selects the fully drawn frame.

    ``frame_marker=None`` means the CLI emits no frame boundaries, so the whole
    stream replays as one chunk and only the final screen is examined. That is
    a real loss of the longest-wins protection above, not a neutral setting --
    it is safe only for a CLI whose value survives to the last frame or is
    recoverable from an OSC 8 hyperlink instead.
    """
    screen = pyte.Screen(columns, lines)
    stream = pyte.Stream(screen)
    best_terminated: str | None = None
    best_any: str | None = None
    frames = raw_output.split(frame_marker) if frame_marker else [raw_output]
    for frame_chunk in frames:
        stream.feed(frame_chunk)
        extracted = extract_value_from_screen_rows(list(screen.display), start_regex, continuation_regex)
        if extracted is None:
            continue
        value, is_terminated = extracted
        if best_any is None or len(value) > len(best_any):
            best_any = value
        if is_terminated and (best_terminated is None or len(value) > len(best_terminated)):
            best_terminated = value
    return best_terminated if best_terminated is not None else best_any


@pure
def extract_hyperlink_value(raw_output: str, target_regex: re.Pattern[str]) -> str | None:
    """Pull a value out of an OSC 8 hyperlink target in the raw stream.

    When a CLI renders a URL as a terminal hyperlink, the (invisible) target
    carries it in full with no width-wrapping, unlike the visible label the
    renderer hard-wraps at the terminal width. Only *terminated* sequences
    match, so a half-received target is never returned.

    ``target_regex`` is matched against the target and its match is what gets
    returned -- not the whole target -- because a target can carry surrounding
    text, and the caller's strict pattern is the definition of the value.
    """
    for match in OSC8_HYPERLINK_REGEX.finditer(raw_output):
        target_match = target_regex.search(match.group(1))
        if target_match is not None:
            return target_match.group(0)
    return None


def drain_pty_stream_until_quiet(process: Any, consumed: str, quiet_seconds: float, deadline_seconds: float) -> str:
    """Read PTY output until no chunk arrives for `quiet_seconds`.

    Used to detect the end of a CLI's paste-echo burst before sending Enter as
    its own keystroke -- an Ink input treats a rapid burst of characters as a
    paste, so an Enter arriving in the same burst lands in the field as content
    instead of submitting. EOF and the overall deadline both end the wait;
    everything read is appended to `consumed` so the session output stays
    complete (a caller may later scrape the credential out of it).
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            chunk = process.read_nonblocking(size=65536, timeout=quiet_seconds)
        except pexpect.TIMEOUT:
            return consumed
        except pexpect.EOF:
            return consumed
        consumed = consumed + (chunk or "")
    return consumed


def drain_pty_stream(
    process: Any,
    consumed: str,
    is_complete: Callable[[str], bool],
    *,
    deadline_seconds: float = DEFAULT_DRAIN_DEADLINE_SECONDS,
    read_seconds: float = DEFAULT_DRAIN_READ_SECONDS,
) -> str:
    """Keep reading PTY output until `is_complete(consumed)` or a deadline.

    `process.expect` returns as soon as its trigger pattern matches, which
    can be mid-escape-sequence or mid-render-frame, so the buffer may hold
    only a prefix of the value being extracted. A CLI animates its spinner
    indefinitely, so there is no reliable quiet gap; completion is judged by
    the caller's predicate, with a hard deadline as backstop.
    """
    deadline = time.monotonic() + deadline_seconds
    while not is_complete(consumed) and time.monotonic() < deadline:
        try:
            chunk = process.read_nonblocking(size=65536, timeout=read_seconds)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
        consumed = consumed + (chunk or "")
    return consumed
