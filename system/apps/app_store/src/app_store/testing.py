"""Test doubles for the App Store: a runner, an installer, supervisord, the registry, a port probe and a resource
reader that record what they are asked and answer from fields; a fetcher of template sizes; and an engine over a
temporary workspace."""

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import Field

from app_manifest.registry import RegistryRow
from app_store.added_apps import AddedAppStore
from app_store.adding import AppAdder
from app_store.catalog import AppCatalog
from app_store.catalog import GithubBuildRecipe
from app_store.catalog import ImageSiteRecipe
from app_store.catalog import NodeRecipe
from app_store.catalog import PythonGitRecipe
from app_store.catalog import PythonRecipe
from app_store.catalog import load_app_catalog
from app_store.command_finder import CommandFinder
from app_store.command_finder import CommandWriterInterface
from app_store.command_finder import FailedTry
from app_store.command_finder import FinderError
from app_store.command_finder import PackageContext
from app_store.command_finder import Proposal
from app_store.command_finder import TrialOutcome
from app_store.command_finder import TrialRunnerInterface
from app_store.engine import InstallEngine
from app_store.errors import InstallError
from app_store.errors import PackageLookupError
from app_store.layout import StoreLayout
from app_store.package_check import LookupCache
from app_store.package_check import PackageChecker
from app_store.package_check import RegistryClientInterface
from app_store.package_check import ResolverInterface
from app_store.process_memory import ProcessMemoryReaderInterface
from app_store.records import RecordStore
from app_store.room import ResourceReaderInterface
from app_store.room import WorkspaceRoom
from app_store.system import AnyRecipe
from app_store.system import CommandRunnerInterface
from app_store.system import PackageInstallerInterface
from app_store.system import PortProbeInterface
from app_store.system import RegistrarInterface
from app_store.system import SupervisorInterface
from app_store.template_sizes import RepositorySizeFetcherInterface

ROOMY: WorkspaceRoom = WorkspaceRoom(disk_free_mb=30_000, memory_available_mb=4_000)


class FakeCommandRunner(CommandRunnerInterface):
    """Records every command, writes it to the log, and answers ``exit_status``."""

    exit_status: int = Field(default=0, description="What every command exits with")
    commands: list[list[str]] = Field(default_factory=list, description="Every command run, in order")
    cwds: list[Path | None] = Field(default_factory=list, description="Where each command ran, in order")

    def run(
        self,
        argv: list[str],
        log_path: Path,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> int:
        self.commands.append(argv)
        self.cwds.append(cwd)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"$ {' '.join(argv)}\n")
        return self.exit_status


class FakePackageInstaller(PackageInstallerInterface):
    """Makes the environment folder (with a stand-in file of ``size_bytes``) and the programs a real install of the
    package would put in it -- one named after the package, and any ``programs_by_package`` names -- or fails when
    told to."""

    is_failing: bool = Field(default=False, description="Whether every install fails")
    size_bytes: int = Field(default=3 * 1024 * 1024, description="How big the stand-in environment is")
    installed: list[str] = Field(default_factory=list, description="Every package spec installed")
    programs_by_package: dict[str, list[str]] = Field(
        default_factory=lambda: {"jupyterlab": ["jupyter-lab"]},
        description="Programs a package installs beyond the one named after it",
    )

    def install(self, recipe: AnyRecipe, env_dir: Path, log_path: Path) -> None:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"installing {recipe.package_spec}\n")
        if self.is_failing:
            raise InstallError(f"Installing {recipe.package_spec} failed (see the install log).")
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "package.bin").write_bytes(b"\0" * self.size_bytes)
        if isinstance(recipe, GithubBuildRecipe | ImageSiteRecipe):
            bin_dir = env_dir / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            for tool in recipe.node_tools:
                (bin_dir / tool.rsplit("@", 1)[0].rsplit("/", 1)[-1]).write_text("#!/bin/sh\n")
            (env_dir / "site").mkdir(exist_ok=True)
            (env_dir / "site" / "index.html").write_text("<!doctype html>")
            self.installed.append(recipe.package_spec)
            return
        if isinstance(recipe, PythonRecipe | PythonGitRecipe):
            name = (
                recipe.requirement.split("==", 1)[0]
                if isinstance(recipe, PythonRecipe)
                else recipe.repository.rsplit("/", 1)[-1]
            )
            bin_dir = env_dir / "bin"
            (bin_dir).mkdir(parents=True, exist_ok=True)
            (bin_dir / "python").write_text("")
        else:
            name = (
                recipe.package.rsplit("@", 1)[0]
                if isinstance(recipe, NodeRecipe)
                else recipe.repository.rsplit("/", 1)[-1]
            )
            bin_dir = env_dir / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
        for program in (name.rsplit("/", 1)[-1], *self.programs_by_package.get(name, [])):
            (bin_dir / program).write_text("#!/bin/sh\n")
        self.installed.append(recipe.package_spec)


class FakeSupervisor(SupervisorInterface):
    """Remembers which programs it has from the drop-ins it was told about."""

    dropin_dir: Path = Field(description="Where the drop-ins are")
    programs: set[str] = Field(default_factory=set, description="Programs supervisord has now")
    calls: list[str] = Field(default_factory=list, description="Every call, as 'apply <p>', 'stop <p>' or 'start <p>'")
    is_refusing: bool = Field(default=False, description="Whether apply_program_change raises")

    def apply_program_change(self, program: str) -> None:
        self.calls.append(f"apply {program}")
        if self.is_refusing:
            raise InstallError(f"supervisord refused update {program}")
        if (self.dropin_dir / f"{program}.conf").exists():
            self.programs.add(program)
        else:
            self.programs.discard(program)

    def stop(self, program: str) -> None:
        self.calls.append(f"stop {program}")

    def start(self, program: str) -> None:
        self.calls.append(f"start {program}")
        if self.is_refusing:
            raise InstallError(f"supervisord refused start {program}")


class FakeRegistrar(RegistrarInterface):
    unregistered: list[str] = Field(default_factory=list, description="Every name unregistered")

    def unregister(self, name: str) -> None:
        self.unregistered.append(name)


class FakePortProbe(PortProbeInterface):
    is_answering: bool = Field(default=True, description="Whether every app answers")
    probed: list[int] = Field(default_factory=list, description="Every port probed")

    def wait_until_answering(self, port: int, timeout_seconds: float) -> bool:
        self.probed.append(port)
        return self.is_answering


class FakeResourceReader(ResourceReaderInterface):
    room: WorkspaceRoom = Field(default=ROOMY, description="What it reads")

    def read_room(self) -> WorkspaceRoom:
        return self.room


class FakeProcessMemoryReader(ProcessMemoryReaderInterface):
    memory_by_program: dict[str, int] = Field(default_factory=dict, description="Each running program's memory")
    pid_by_program: dict[str, int] = Field(default_factory=dict, description="Each running program's pid")

    def program_pid(self, program: str) -> int | None:
        if program in self.pid_by_program:
            return self.pid_by_program[program]
        return 1000 + len(program) if program in self.memory_by_program else None

    def pid_memory_mb(self, pid: int) -> int:
        for program, memory in self.memory_by_program.items():
            if self.program_pid(program) == pid:
                return memory
        return 0


class FakeRepositorySizeFetcher(RepositorySizeFetcherInterface):
    size_by_url: dict[str, int] = Field(default_factory=dict, description="Each API URL's size; absent fails")
    fetched: list[str] = Field(default_factory=list, description="Every API URL fetched")

    def fetch_size_kb(self, api_url: str) -> int | None:
        self.fetched.append(api_url)
        return self.size_by_url.get(api_url)


def make_registry_row(name: str, port: int, **overrides: object) -> RegistryRow:
    return RegistryRow.model_validate({"name": name, "url": f"http://localhost:{port}", **overrides})


def build_test_engine(
    tmp_path: Path,
    registry_rows: list[RegistryRow] | None = None,
    bound_ports: set[int] | None = None,
    curated: AppCatalog | None = None,
) -> InstallEngine:
    """An engine whose workspace is ``tmp_path`` (a supervisord config and drop-in folder, an empty registry), using
    the shipped catalog (or ``curated``) and every fake above."""
    repo_root = tmp_path / "workspace"
    (repo_root / "system" / "supervisord.conf.d").mkdir(parents=True, exist_ok=True)
    (repo_root / "system" / "supervisord.conf").write_text("[supervisord]\n")
    layout = StoreLayout(data_dir=repo_root / "data" / ".apps" / "app-store", repo_root=repo_root)
    rows = registry_rows if registry_rows is not None else []
    taken = bound_ports if bound_ports is not None else set()
    return InstallEngine(
        curated=curated if curated is not None else load_app_catalog(),
        added=AddedAppStore(path=layout.added_apps_path),
        layout=layout,
        records=RecordStore(path=layout.records_path),
        installer=FakePackageInstaller(),
        runner=FakeCommandRunner(),
        supervisor=FakeSupervisor(dropin_dir=layout.dropin_dir),
        registrar=FakeRegistrar(),
        probe=FakePortProbe(),
        resources=FakeResourceReader(),
        read_registry_rows=lambda: rows,
        program_log_directory=tmp_path / "program-logs",
        memory_reader=FakeProcessMemoryReader(),
        is_bound=lambda port: port in taken,
    )


class FakeRegistryClient(RegistryClientInterface):
    """Answers registry URLs from a table (None, a 404, for one not in it); raises for URLs in ``unreachable``."""

    documents: dict[str, dict[str, Any]] = Field(default_factory=dict, description="What each URL answers")
    unreachable: set[str] = Field(default_factory=set, description="URLs that fail as if offline")
    fetched: list[str] = Field(default_factory=list, description="Every URL fetched")

    def get_json(self, url: str) -> dict[str, Any] | None:
        self.fetched.append(url)
        if url in self.unreachable:
            raise PackageLookupError(f"could not reach {url}")
        return self.documents.get(url)


class FakeResolver(ResolverInterface):
    """Resolves from tables; a requirement not in them fails as resolution does."""

    python_sets: dict[str, list[tuple[str, str]]] = Field(default_factory=dict, description="By requirement")
    npm_sets: dict[str, list[tuple[str, str]]] = Field(default_factory=dict, description="By package spec")

    def resolve_python(self, requirement: str, python: str) -> list[tuple[str, str]]:
        if requirement not in self.python_sets:
            raise PackageLookupError(f"resolving failed: no solution for {requirement}")
        return self.python_sets[requirement]

    def resolve_npm(self, package_spec: str) -> list[tuple[str, str]]:
        if package_spec not in self.npm_sets:
            raise PackageLookupError(f"resolving failed: no solution for {package_spec}")
        return self.npm_sets[package_spec]


def pypi_project(name: str, versions: list[str], summary: str = "A demo app.") -> dict[str, Any]:
    """A PyPI project document whose latest release is the last of ``versions``."""
    return {
        "info": {"name": name, "version": versions[-1], "summary": summary, "author": "Someone", "license": "MIT"},
        "releases": {version: [] for version in versions},
    }


def pypi_release(name: str, version: str, wheel_size: int) -> dict[str, Any]:
    """A PyPI release document with one pure-Python wheel and a source archive."""
    return {
        "info": {"name": name, "version": version},
        "urls": [
            {"filename": f"{name}-{version}-py3-none-any.whl", "packagetype": "bdist_wheel", "size": wheel_size},
            {"filename": f"{name}-{version}.tar.gz", "packagetype": "sdist", "size": wheel_size * 10},
        ],
    }


def demo_checker(tmp_path: Path) -> PackageChecker:
    """A checker over fake registries holding a Python package ``demo`` (1.0, 1.1; it pulls in ``helper``) and an npm
    package ``demo-server`` (2.0.0; it pulls in ``mime``, which reports no size)."""
    registry = FakeRegistryClient(
        documents={
            "https://pypi.org/pypi/demo/json": pypi_project("demo", ["1.0", "1.1"]),
            "https://pypi.org/pypi/demo/1.1/json": pypi_release("demo", "1.1", 3 * 1024 * 1024),
            "https://pypi.org/pypi/demo/1.0/json": pypi_release("demo", "1.0", 2 * 1024 * 1024),
            "https://pypi.org/pypi/helper/2.0/json": pypi_release("helper", "2.0", 1024 * 1024),
            "https://registry.npmjs.org/demo-server": {
                "dist-tags": {"latest": "2.0.0"},
                "versions": {"2.0.0": {"description": "Serves files.", "license": "MIT", "author": {"name": "A"}}},
            },
            "https://registry.npmjs.org/demo-server/2.0.0": {"dist": {"unpackedSize": 5 * 1024 * 1024}},
            "https://registry.npmjs.org/mime/1.6.0": {"dist": {}},
        }
    )
    resolver = FakeResolver(
        python_sets={"demo==1.1": [("demo", "1.1"), ("helper", "2.0")], "demo==1.0": [("demo", "1.0")]},
        npm_sets={"demo-server@2.0.0": [("demo-server", "2.0.0"), ("mime", "1.6.0")]},
    )
    return PackageChecker(registry=registry, resolver=resolver, cache=LookupCache(path=tmp_path / "lookups.json"))


def build_test_adder(engine: InstallEngine, tmp_path: Path) -> AppAdder:
    return AppAdder(engine=engine, checker=demo_checker(tmp_path), exclude_path=tmp_path / "info" / "exclude")


class FakeCommandWriter(CommandWriterInterface):
    """Answers each try with the next of ``proposals`` (a FinderError once they run out), remembering what it saw."""

    proposals: list[Proposal] = Field(default_factory=list, description="The answers, in order")
    contexts: list[PackageContext] = Field(default_factory=list, description="What each try was shown")
    failures_seen: list[list[FailedTry]] = Field(default_factory=list, description="The failed tries each try saw")

    def propose(self, context: PackageContext, failed: list[FailedTry], log_path: Path) -> Proposal:
        self.contexts.append(context)
        self.failures_seen.append(list(failed))
        if len(self.failures_seen) > len(self.proposals):
            raise FinderError("The AI's answer couldn't be read.")
        return self.proposals[len(self.failures_seen) - 1]


class FakeTrialRunner(TrialRunnerInterface):
    """Answers each launch with the next of ``outcomes``, remembering the argv it was asked to run."""

    outcomes: list[TrialOutcome] = Field(default_factory=list, description="The outcomes, in order")
    launched: list[list[str]] = Field(default_factory=list, description="Every argv run")

    def run(self, argv: list[str], cwd: Path, port: int, log_path: Path, cancelled: threading.Event) -> TrialOutcome:
        self.launched.append(argv)
        return self.outcomes[len(self.launched) - 1]


def build_test_finder(
    engine: InstallEngine,
    tmp_path: Path,
    proposals: list[Proposal] | None = None,
    outcomes: list[TrialOutcome] | None = None,
    finder_type: type[CommandFinder] = CommandFinder,
) -> CommandFinder:
    return finder_type(
        engine=engine,
        checker=demo_checker(tmp_path),
        writer=FakeCommandWriter(proposals=proposals or []),
        trial_runner=FakeTrialRunner(outcomes=outcomes or []),
        read_program_help=lambda program, cwd: f"usage: {program.name} [--port PORT] [--host HOST]",
    )
