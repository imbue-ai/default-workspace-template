"""The store's reach into the workspace, each behind a small interface so tests can use fakes: running a command into
an install log, installing a package recipe, supervisord, the app registry, and probing a port."""

import os
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from typing import assert_never

import httpx
from loguru import logger
from pydantic import Field

from app_manifest.registry import FORWARD_PORT_SCRIPT
from app_store.catalog import GithubBuildRecipe
from app_store.catalog import ImageSiteRecipe
from app_store.catalog import NodeGitRecipe
from app_store.catalog import NodeRecipe
from app_store.catalog import PythonGitRecipe
from app_store.catalog import PythonRecipe
from app_store.catalog import render_argv
from app_store.errors import InstallError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

# A package install that has not finished in this long has hung.
INSTALL_TIMEOUT_SECONDS: Final[float] = 20 * 60.0
SUPERVISORCTL_TIMEOUT_SECONDS: Final[float] = 120.0
REGISTRY_TIMEOUT_SECONDS: Final[float] = 30.0
PORT_PROBE_INTERVAL_SECONDS: Final[float] = 0.5
PORT_PROBE_REQUEST_TIMEOUT_SECONDS: Final[float] = 2.0


class CommandRunnerInterface(MutableModel, ABC):
    """Runs one command to completion, appending everything it prints to a log file."""

    @abstractmethod
    def run(
        self,
        argv: list[str],
        log_path: Path,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> int:
        """The command's exit status; -1 when it could not be started or timed out (both written to the log)."""


class SubprocessCommandRunner(CommandRunnerInterface):
    """The production runner."""

    def run(
        self,
        argv: list[str],
        log_path: Path,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"$ {' '.join(argv)}\n")
            log.flush()
            try:
                completed = subprocess.run(
                    argv,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    timeout=timeout_seconds,
                    env={**os.environ, **env} if env is not None else None,
                    cwd=cwd,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                log.write(f"\n[app-store] gave up after {timeout_seconds:.0f}s\n")
                return -1
            except OSError as e:
                log.write(f"\n[app-store] could not run {argv[0]}: {e}\n")
                return -1
            log.write(f"[app-store] exit status {completed.returncode}\n")
            return completed.returncode


AnyRecipe = PythonRecipe | NodeRecipe | PythonGitRecipe | NodeGitRecipe | GithubBuildRecipe | ImageSiteRecipe


class PackageInstallerInterface(MutableModel, ABC):
    """Installs a recipe into an app's environment folder."""

    @abstractmethod
    def install(self, recipe: AnyRecipe, env_dir: Path, log_path: Path) -> None:
        """Make ``env_dir`` hold the recipe's package; raises InstallError (already logged) when it cannot."""


SOURCE_DIRNAME: Final[str] = "source"
SITE_DIRNAME: Final[str] = "site"


class InstallStep(FrozenModel):
    """One command an install runs: its argv, where it runs, and what it adds to the environment."""

    argv: tuple[str, ...] = Field(description="The command")
    cwd: Path | None = Field(default=None, description="Where it runs; None for the store's own folder")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables it adds")


class CommandPackageInstaller(PackageInstallerInterface):
    """The production installer: ``uv`` for Python recipes, ``npm`` for Node ones, for a GitHub build the pinned
    tools, the exact commit and its build, and for a ready-made image its server and the site's layer -- each into the
    app's own folder."""

    runner: CommandRunnerInterface = Field(frozen=True, description="Runs the installer commands")

    def install(self, recipe: AnyRecipe, env_dir: Path, log_path: Path) -> None:
        for step in install_steps(recipe, env_dir):
            status = self.runner.run(list(step.argv), log_path, INSTALL_TIMEOUT_SECONDS, step.env or None, step.cwd)
            if status != 0:
                raise InstallError(f"Installing {recipe.package_spec} failed (see the install log).")
        if isinstance(recipe, GithubBuildRecipe):
            keep_built_site(recipe, env_dir)


def install_steps(recipe: AnyRecipe, env_dir: Path) -> list[InstallStep]:
    """The commands that install ``recipe`` into ``env_dir``, in order."""
    match recipe:
        case PythonRecipe() | PythonGitRecipe():
            return [
                InstallStep(argv=("uv", "venv", "--quiet", "--python", recipe.python, str(env_dir))),
                InstallStep(
                    argv=("uv", "pip", "install", "--python", str(env_dir / "bin" / "python"), recipe.requirement)
                ),
            ]
        case NodeRecipe() | NodeGitRecipe():
            return [
                InstallStep(
                    argv=("npm", "install", "--prefix", str(env_dir), "--no-audit", "--no-fund", recipe.package)
                )
            ]
        case GithubBuildRecipe():
            source = env_dir / SOURCE_DIRNAME
            steps = [
                InstallStep(
                    argv=("npm", "install", "--prefix", str(env_dir), "--no-audit", "--no-fund", *recipe.node_tools)
                ),
                InstallStep(argv=("git", "init", "--quiet", str(source))),
                # Fetched by its hash: what is built is exactly the commit that was reviewed, whatever the tag says now.
                InstallStep(
                    argv=(
                        "git",
                        "-C",
                        str(source),
                        "fetch",
                        "--quiet",
                        "--depth",
                        "1",
                        f"{recipe.repository}.git",
                        recipe.commit,
                    )
                ),
                InstallStep(
                    argv=(
                        "git",
                        "-C",
                        str(source),
                        "-c",
                        "advice.detachedHead=false",
                        "checkout",
                        "--quiet",
                        "--detach",
                        recipe.commit,
                    )
                ),
            ]
            build_env = dict(
                zip(
                    recipe.build_env.keys(),
                    render_argv(tuple(recipe.build_env.values()), 0, env_dir, env_dir),
                    strict=True,
                )
            )
            for step in recipe.build:
                steps.append(
                    InstallStep(argv=tuple(render_argv(step, 0, env_dir, env_dir)), cwd=source, env=build_env)
                )
            return steps
        case ImageSiteRecipe():
            return [
                InstallStep(
                    argv=("npm", "install", "--prefix", str(env_dir), "--no-audit", "--no-fund", *recipe.node_tools)
                ),
                # The store's own Python runs the download, so it needs nothing the app's environment lacks.
                InstallStep(
                    argv=(
                        sys.executable,
                        "-m",
                        "app_store.fetch_image_layer",
                        recipe.image,
                        recipe.layer,
                        recipe.path,
                        str(env_dir / SITE_DIRNAME),
                    )
                ),
            ]
        case _ as unreachable:
            assert_never(unreachable)


def keep_built_site(recipe: GithubBuildRecipe, env_dir: Path) -> None:
    """Keep only the built site, as ``site`` in the environment, and delete the checkout and its build tooling."""
    source = env_dir / SOURCE_DIRNAME
    built = source / recipe.output
    if not (built / "index.html").is_file():
        raise InstallError(f"The build finished but made no site at {recipe.output} (see the install log).")
    site = env_dir / SITE_DIRNAME
    shutil.rmtree(site, ignore_errors=True)
    shutil.move(built, site)
    shutil.rmtree(source, ignore_errors=True)


class SupervisorInterface(MutableModel, ABC):
    """The slice of supervisord the store drives."""

    @abstractmethod
    def apply_program_change(self, program: str) -> None:
        """Re-read the config and add, change, or remove ``program`` to match its drop-in (``reread`` + ``update``).
        Raises InstallError when supervisord refuses."""

    @abstractmethod
    def stop(self, program: str) -> None:
        """Stop ``program`` if it runs; a program supervisord does not know is not an error."""

    @abstractmethod
    def start(self, program: str) -> None:
        """Start ``program`` if it is stopped. Raises InstallError when supervisord refuses."""


class SupervisorctlSupervisor(SupervisorInterface):
    """The production supervisor: ``supervisorctl``."""

    def apply_program_change(self, program: str) -> None:
        for argv in (["supervisorctl", "reread"], ["supervisorctl", "update", program]):
            completed = _run_quietly(argv, SUPERVISORCTL_TIMEOUT_SECONDS)
            if completed is None or completed.returncode != 0 or "ERROR" in completed.stdout:
                output = "" if completed is None else completed.stdout.strip()
                raise InstallError(f"supervisord refused {' '.join(argv[1:])}: {output}")

    def stop(self, program: str) -> None:
        completed = _run_quietly(["supervisorctl", "stop", program], SUPERVISORCTL_TIMEOUT_SECONDS)
        if completed is not None and completed.returncode != 0 and "no such" not in completed.stdout.lower():
            logger.warning("supervisorctl stop {} answered: {}", program, completed.stdout.strip())

    def start(self, program: str) -> None:
        completed = _run_quietly(["supervisorctl", "start", program], SUPERVISORCTL_TIMEOUT_SECONDS)
        output = "" if completed is None else completed.stdout
        if completed is None or (completed.returncode != 0 and "already started" not in output):
            raise InstallError(f"supervisord refused start {program}: {output.strip()}")


class RegistrarInterface(MutableModel, ABC):
    """Removes an app's row from the app registry."""

    @abstractmethod
    def unregister(self, name: str) -> None:
        """Drop ``name``'s row (``forward_port.py --remove``); a name with no row is not an error."""


class ForwardPortRegistrar(RegistrarInterface):
    """The production registrar."""

    def unregister(self, name: str) -> None:
        completed = _run_quietly(
            ["python3", str(FORWARD_PORT_SCRIPT), "--name", name, "--remove"], REGISTRY_TIMEOUT_SECONDS
        )
        if completed is None or completed.returncode != 0:
            output = "" if completed is None else completed.stdout.strip()
            logger.warning("Could not unregister {}: {}", name, output)


class PortProbeInterface(MutableModel, ABC):
    """Waits for an app to answer."""

    @abstractmethod
    def wait_until_answering(self, port: int, timeout_seconds: float) -> bool:
        """Whether ``127.0.0.1:port`` answered HTTP within the timeout."""


class HttpPortProbe(PortProbeInterface):
    """The production probe: any HTTP answer below 500 on ``/`` counts."""

    def wait_until_answering(self, port: int, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        pause = threading.Event()
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/", timeout=PORT_PROBE_REQUEST_TIMEOUT_SECONDS)
            except httpx.HTTPError:
                response = None
            if response is not None and response.status_code < 500:
                return True
            pause.wait(PORT_PROBE_INTERVAL_SECONDS)
        return False


def _run_quietly(argv: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_seconds, check=False, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("Could not run {}: {}", " ".join(argv), e)
        return None
