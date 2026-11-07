"""The claim: what turns a blank sandbox (one of the warm pool's, or one made on demand) into one user's for one
sign-in session.

The connector writes ``/run/auth-sandbox-claim.json`` when it hands the sandbox to a user:

    {"authorized_key": "<the user's OpenSSH public key line>", "screen_token_sha256": "<lower-case hex>",
     "expires_at": <unix seconds>}

``auth-sandbox-await-claim``, which the boot script starts before supervisord, polls for it, installs the first one
that validates as the whole of owner-exec's ``authorized_keys`` and as the Screen's owner token digest file, and
returns at ``expires_at``; the boot script answers that by stopping supervisord and exiting, which ends the sandbox.
A claim that does not validate is logged and ignored, so the sandbox stays unusable and the connector's sweep
retires it; the watcher reads the file again only once it changes.
"""

import json
import math
import os
import re
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from auth_sandbox.errors import ClaimFileError
from auth_sandbox.interfaces import ClockInterface
from auth_sandbox.runtime_files import write_private_file

CLAIM_POLL_INTERVAL_SECONDS: Final[float] = 0.25

_SHA256_HEX_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")


class Claim(FrozenModel):
    """The claim file, validated."""

    authorized_key: str = Field(description="The user's OpenSSH public key line")
    screen_token_sha256: str = Field(
        description="The lower-case hex SHA-256 of the Screen's owner token"
    )
    expires_at: float = Field(description="When the session ends, in unix seconds")


class ClaimPaths(FrozenModel):
    """Where the claim is read from and installed to."""

    claim_path: Path = Field(description="The file the connector writes")
    authorized_keys_path: Path = Field(description="owner-exec's authorized_keys")
    screen_token_sha256_path: Path = Field(
        description="The digest file the Screen's gate reads per request"
    )


class _FileStamp(FrozenModel):
    """Enough of a file's metadata to tell a rewrite (the connector's rename makes a new inode) from the same file."""

    inode: int = Field(description="st_ino")
    modified_ns: int = Field(description="st_mtime_ns")
    size: int = Field(description="st_size")


@pure
def parse_claim(text: str) -> Claim:
    """Parse the claim file, refusing a value that spans lines: each lands in a file a reader splits on lines or a
    header, so a line break would add a key or a directive the connector never sent.

    Raises:
        ClaimFileError: when the text is not that shape.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as e:
        raise ClaimFileError(
            f"the claim is not valid JSON: {e.msg} at line {e.lineno}"
        ) from e
    if not isinstance(document, dict):
        raise ClaimFileError("the claim must hold a JSON object")
    authorized_key = document.get("authorized_key")
    if not isinstance(authorized_key, str) or not authorized_key.strip():
        raise ClaimFileError("the claim's authorized_key must be a non-empty string")
    if "\n" in authorized_key or "\r" in authorized_key:
        raise ClaimFileError("the claim's authorized_key must be one line")
    screen_token_sha256 = document.get("screen_token_sha256")
    if (
        not isinstance(screen_token_sha256, str)
        or _SHA256_HEX_PATTERN.fullmatch(screen_token_sha256) is None
    ):
        raise ClaimFileError(
            "the claim's screen_token_sha256 must be 64 lower-case hex digits"
        )
    expires_at = document.get("expires_at")
    # bool is an int to Python, and json.loads reads NaN and Infinity.
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or not math.isfinite(expires_at)
        or expires_at <= 0
    ):
        raise ClaimFileError("the claim's expires_at must be a positive unix time")
    return Claim(
        authorized_key=authorized_key,
        screen_token_sha256=screen_token_sha256,
        expires_at=float(expires_at),
    )


def install_claim(claim: Claim, paths: ClaimPaths) -> None:
    """Admit the claiming user: their key as the whole of owner-exec's ``authorized_keys``, and the owner token's
    digest where the Screen's gate reads it."""
    write_private_file(
        paths.authorized_keys_path, (claim.authorized_key + "\n").encode("utf-8")
    )
    write_private_file(
        paths.screen_token_sha256_path,
        (claim.screen_token_sha256 + "\n").encode("ascii"),
    )


def _stamp(path: Path) -> _FileStamp | None:
    try:
        status = os.stat(path)
    except FileNotFoundError:
        return None
    return _FileStamp(
        inode=status.st_ino, modified_ns=status.st_mtime_ns, size=status.st_size
    )


def _read_claim(path: Path) -> Claim | None:
    """The claim at ``path``, or None (logged) when it cannot be read or does not validate."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        logger.error("Ignoring the claim at {}: it cannot be read: {}", path, e)
        return None
    try:
        return parse_claim(text)
    except ClaimFileError as e:
        logger.error("Ignoring the claim at {}: {}", path, e)
        return None


def await_claim(
    claim_path: Path, clock: ClockInterface, poll_interval_seconds: float
) -> Claim:
    """Poll until the claim file holds a claim that validates; a file that does not is read again only once it
    changes, so a refused claim is logged once."""
    refused_stamp: _FileStamp | None = None
    claim: Claim | None = None
    while claim is None:
        stamp = _stamp(claim_path)
        if stamp is not None and stamp != refused_stamp:
            claim = _read_claim(claim_path)
            refused_stamp = stamp
        if claim is None:
            clock.pause(poll_interval_seconds)
    return claim


def serve_claim(
    paths: ClaimPaths, clock: ClockInterface, poll_interval_seconds: float
) -> None:
    """Wait for the claim, install it, and return at its expiry. A claim that has already expired is not
    installed: no one is admitted to a session that is over."""
    claim = await_claim(paths.claim_path, clock, poll_interval_seconds)
    if claim.expires_at <= clock.now():
        logger.error(
            "The claim expired at {} before it arrived; ending the sandbox without admitting anyone",
            claim.expires_at,
        )
        return
    install_claim(claim, paths)
    logger.info("Claimed; the session ends at {}", claim.expires_at)
    remaining = claim.expires_at - clock.now()
    while remaining > 0:
        clock.pause(remaining)
        remaining = claim.expires_at - clock.now()
    logger.info("The session expired at {}", claim.expires_at)
