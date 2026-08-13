"""Tests for the service launch wrapper.

The wrapper sets its own memory-shedding band from a named service key, then
execs the service command with its args untouched. We verify the exec + arg
forwarding end to end via a subprocess with a fake command that records what it
was run with, and -- on a host with a writable ``/proc/self/oom_score_adj`` --
that the band is actually applied and survives the exec.
"""

from __future__ import annotations

import configparser
import glob
import os
import subprocess
import sys
from pathlib import Path

from oom_priority import bands

_SCRIPT = Path(__file__).parent / "oom_tag_service.py"
_SUPERVISORD_CONF = Path(__file__).resolve().parents[3] / "supervisord.conf"

_PROC_OOM = Path("/proc/self/oom_score_adj")
_HAS_WRITABLE_PROC_OOM = os.access(_PROC_OOM, os.W_OK)

# The key a user-created program passes to the wrapper to declare itself as one.
_USER_SERVICE_KEY = "user"


def _command_by_supervisord_program() -> dict[str, str]:
    """Every program / event listener the workspace defines, and its command.

    supervisord's config is an ini file, so ``configparser`` reads it directly:
    that skips the file's prose comments (which mention the wrapper by name
    without invoking it) and folds continuation lines, both of which a
    line-by-line scan has to special-case. Interpolation is off because
    supervisord's own ``%(ENV_x)s`` syntax is not configparser's.

    Programs live one per file under ``supervisord.conf.d/``, so the main config
    alone declares only ``system_interface``. ``configparser`` does not follow
    supervisord's ``[include]`` directive -- that is a supervisord feature, not
    a configparser one -- so the globs are expanded here. Reading only the main
    config would leave the band checks below asserting over a single program
    while appearing to pass, which is exactly the silent gap they exist to
    close.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_SUPERVISORD_CONF)
    conf_dir = _SUPERVISORD_CONF.parent
    for pattern in (parser.get("include", "files", fallback="") or "").split():
        # supervisord joins each pattern to the directory of the config
        # declaring it, and expands %(here)s to that same directory;
        # interpolation=None leaves that token verbatim, so substitute it.
        expanded = str(conf_dir / pattern.replace("%(here)s", str(conf_dir)))
        parser.read(sorted(glob.glob(expanded)))
    return {
        section.partition(":")[2]: parser[section].get("command", "")
        for section in parser.sections()
        if section.startswith(("program:", "eventlistener:"))
    }


def _service_key_of(command: str) -> str | None:
    """The service key ``command`` passes to this wrapper, or None if it does not use it."""
    _, marker, rest = command.partition("oom_tag_service.py ")
    if not marker or not rest.split():
        return None
    return rest.split()[0]


def _fake_command(tmp_path: Path) -> tuple[Path, Path]:
    """A fake service command that records its args and its own
    ``oom_score_adj`` (so we can observe both the exec forwarding and the tag
    that survived it)."""
    out = tmp_path / "recorded.txt"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "fake-service"
    fake.write_text(
        "#!/bin/sh\n"
        'printf "args:%s\\n" "$*" > ' + str(out) + "\n"
        "cat /proc/self/oom_score_adj >> " + str(out) + " 2>/dev/null || true\n"
    )
    fake.chmod(0o755)
    return bindir, out


def _run(script_args: list[str], bindir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *script_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_execs_the_command_forwarding_its_args(tmp_path: Path) -> None:
    bindir, out = _fake_command(tmp_path)
    result = _run(["system_interface", "fake-service", "--flag", "value"], bindir)
    assert result.returncode == 0, result.stderr
    assert out.read_text().splitlines()[0] == "args:--flag value"


def test_unknown_service_key_execs_and_defaults_to_the_user_band(
    tmp_path: Path,
) -> None:
    # An unrecognized key must fail expendable (the user-service band), never
    # keep the fully-protected inherited default.
    bindir, out = _fake_command(tmp_path)
    result = _run(["not-a-real-service", "fake-service", "arg"], bindir)
    assert result.returncode == 0, result.stderr
    recorded = out.read_text().splitlines()
    assert recorded[0] == "args:arg"
    assert "unknown service band" in result.stderr
    if _HAS_WRITABLE_PROC_OOM:
        assert recorded[1] == str(bands.USER_SERVICE)


def test_missing_command_exits_nonzero_with_usage(tmp_path: Path) -> None:
    bindir, _ = _fake_command(tmp_path)
    result = _run(["system_interface"], bindir)
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_every_service_key_in_supervisord_conf_has_a_band() -> None:
    # A built-in service whose key is missing from SERVICE_BANDS silently falls
    # back to USER_SERVICE (200) -- which sits ABOVE every built-in, so it would
    # be shed before all of them, the opposite of what a built-in wants. The only
    # runtime signal is a warning on that service's own stderr, which is easy to
    # miss for months (this is what happened to `xvfb`). Catch it here instead.
    keys = [
        key
        for key in (_service_key_of(command) for command in _command_by_supervisord_program().values())
        if key is not None
    ]
    assert keys, f"no oom_tag_service.py invocations found in {_SUPERVISORD_CONF}"
    unbanded = sorted({key for key in keys if key not in bands.SERVICE_BANDS})
    assert not unbanded, (
        f"supervisord.conf passes service keys with no SERVICE_BANDS entry: {unbanded}. "
        "Add each to SERVICE_BANDS (or pass 'user' if it really is user-created)."
    )


def test_every_built_in_supervisord_program_has_an_explicit_band() -> None:
    # The wrapper-key check above only sees programs that opted into the tagging
    # prefix. A program that skips it is not untagged -- the backstop listener
    # tags it from its *program name* instead, and an unrecognized name resolves
    # to USER_SERVICE (200). For a user-created service that fail-expendable
    # default is the point; for a built-in it is silently wrong, and here it is
    # actively harmful, because the backstop *raises* the process to it. That is
    # how `env-converge` -- the one-shot first-boot provisioner that must stay
    # PROTECTED, since a shed mid-run leaves the rootfs half-provisioned with
    # autorestart=false and nothing to finish it -- was being pushed to 200.
    #
    # So every program must name its band, EXCEPT one that declares itself
    # user-created by passing the `user` key (what the build-app skill scaffolds
    # into a workspace). For those the fallback is not an oversight: the wrapper
    # and the backstop then independently agree on USER_SERVICE, and a user's own
    # app belongs above every built-in, not inside their map.
    command_by_program = _command_by_supervisord_program()
    assert command_by_program, f"no program sections found in {_SUPERVISORD_CONF}"
    implicit = sorted(
        {
            program
            for program, command in command_by_program.items()
            if _service_key_of(command) != _USER_SERVICE_KEY
            and program not in bands.SERVICE_BANDS
            and program not in bands._NON_SERVICE_PROGRAM_BANDS
        }
    )
    assert not implicit, (
        f"supervisord.conf defines built-in programs with no explicit band: {implicit}. "
        "Each falls through to the USER_SERVICE fallback, which sits above every "
        "built-in. Add each to SERVICE_BANDS (a service) or to "
        "_NON_SERVICE_PROGRAM_BANDS (infrastructure or a one-shot) -- or, if it "
        f"really is user-created, pass '{_USER_SERVICE_KEY}' to oom_tag_service.py."
    )


def test_applies_the_service_band_and_it_survives_the_exec(tmp_path: Path) -> None:
    if not _HAS_WRITABLE_PROC_OOM:
        # No writable /proc/self/oom_score_adj (e.g. macOS): tagging is a
        # best-effort no-op, so the end-to-end band check does not apply.
        return
    bindir, out = _fake_command(tmp_path)
    result = _run(["share-gateway", "fake-service"], bindir)
    assert result.returncode == 0, result.stderr
    recorded = out.read_text().splitlines()
    assert recorded[1] == str(bands.SERVICE_BANDS["share-gateway"])
