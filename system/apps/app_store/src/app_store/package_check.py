"""Checking a package a user wants to add, without installing it: which exact version will be pinned, what the
registry says about it, how many packages it pulls in, and roughly how much disk it will take.

Version. Left empty, the latest release is looked up and pinned; given, it must exist on the registry.

Disk, Python. The full set of packages is resolved with ``uv pip compile`` for the target Python on this platform;
then each pinned package's file is looked up in the PyPI JSON API, choosing the file uv would choose -- the wheel
whose tags best match the target interpreter, else the source archive. PyPI serves compressed archives, so their
sum is multiplied by ``PYTHON_UNPACKED_FACTOR`` (measured on this workspace's installs of the curated apps).

Disk, npm. The full set is resolved in a scratch folder with ``npm install --package-lock-only``; then each package's
``dist.unpackedSize`` is read from the registry. That figure is already unpacked, so it is used as it is.

Every lookup is cached (``package_lookups.json`` in the store's state folder; a pinned version's facts never change,
"latest" is kept for an hour), and resolution and lookups run against a time limit. When either fails, the check
still succeeds: the size is unknown, and it says why.
"""

import json
import platform
import subprocess
import tempfile
import threading
import time
from abc import ABC
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from loguru import logger
from packaging.tags import Tag
from packaging.tags import compatible_tags
from packaging.tags import cpython_tags
from packaging.tags import platform_tags
from packaging.utils import InvalidWheelFilename
from packaging.utils import canonicalize_name
from packaging.utils import parse_wheel_filename
from packaging.version import InvalidVersion
from packaging.version import Version
from pydantic import Field
from pydantic import PrivateAttr

from app_store.added_apps import CheckedRequest
from app_store.added_apps import SOURCE_NAMES
from app_store.errors import AddAppRefusedError
from app_store.errors import PackageLookupError
from getting_started.errors import StateFileError
from getting_started.state_files import read_json_object
from getting_started.state_files import write_json_atomic
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

PYPI_ROOT: Final[str] = "https://pypi.org"
NPM_REGISTRY_ROOT: Final[str] = "https://registry.npmjs.org"
LOOKUPS_CACHE_FILENAME: Final[str] = "package_lookups.json"
# How many lookups the cache keeps (each dependency file sized is one); past it the oldest are dropped.
LOOKUP_CACHE_MAX_ENTRIES: Final[int] = 5000
# Installed Python packages take about this many times their (compressed) download, measured on this workspace:
# JupyterLab 3.3x (45.9 MB of files, 151 MB installed) and marimo 3.4x (51.7 MB, 174 MB); Datasette, small enough that
# the environment itself shows, 4.2x (5.0 MB, 21 MB).
PYTHON_UNPACKED_FACTOR: Final[float] = 3.3
RESOLVE_TIMEOUT_SECONDS: Final[float] = 90.0
LOOKUP_TIMEOUT_SECONDS: Final[float] = 15.0
ESTIMATE_BUDGET_SECONDS: Final[float] = 75.0
LATEST_FRESH_SECONDS: Final[float] = 60 * 60.0
LOOKUP_WORKERS: Final[int] = 12
_BYTES_PER_MB: Final[int] = 1024 * 1024


class PackageFacts(FrozenModel):
    """What the registry says about the pinned version."""

    version: str = Field(description="The exact version that will be pinned")
    summary: str = Field(description="Its one-line description; empty when it has none")
    author: str = Field(description="Who publishes it; empty when unknown")
    license: str = Field(description="Its license as the registry gives it; empty when unknown")
    homepage_url: str = Field(description="Its project page (https), or its registry page")
    registry_url: str = Field(description="Its page on the registry, at the pinned version")


class DiskEstimate(FrozenModel):
    """How much disk an added app will take, and how that was worked out."""

    disk_mb: int | None = Field(description="The estimate in MB; null when it could not be worked out")
    package_count: int | None = Field(description="How many packages the install pulls in; null when unresolved")
    basis: str = Field(description="How the figure was worked out, or why it is unknown, in plain words")


class RegistryClientInterface(MutableModel, ABC):
    """Reads package registries. Each answers the JSON document, None for a 404, and raises PackageLookupError when
    the registry could not be reached."""

    @abstractmethod
    def get_json(self, url: str) -> dict[str, Any] | None:
        """The JSON document at ``url``."""


class HttpRegistryClient(RegistryClientInterface):
    def get_json(self, url: str) -> dict[str, Any] | None:
        try:
            response = httpx.get(url, timeout=LOOKUP_TIMEOUT_SECONDS, follow_redirects=True)
        except httpx.HTTPError as e:
            raise PackageLookupError(f"could not reach {url}: {e}") from e
        if response.status_code == 404:
            return None
        if response.is_error:
            raise PackageLookupError(f"{url} answered {response.status_code}")
        try:
            document = response.json()
        except ValueError as e:
            raise PackageLookupError(f"{url} did not answer JSON") from e
        return document if isinstance(document, dict) else None


class ResolverInterface(MutableModel, ABC):
    """Resolves a package to the full pinned set it installs, without installing it."""

    @abstractmethod
    def resolve_python(self, requirement: str, python: str) -> list[tuple[str, str]]:
        """Every (name, version) ``uv pip compile`` pins for ``requirement``; raises PackageLookupError."""

    @abstractmethod
    def resolve_npm(self, package_spec: str) -> list[tuple[str, str]]:
        """Every (name, version) npm's lockfile holds for ``package_spec``; raises PackageLookupError."""


def python_platform() -> str:
    """This machine's platform as uv names it."""
    return f"{platform.machine()}-unknown-linux-gnu"


class CommandResolver(ResolverInterface):
    """The production resolver: ``uv pip compile`` and ``npm install --package-lock-only``, each in a scratch folder
    and under a time limit."""

    timeout_seconds: float = Field(default=RESOLVE_TIMEOUT_SECONDS, frozen=True, description="Per resolution")

    def resolve_python(self, requirement: str, python: str) -> list[tuple[str, str]]:
        with tempfile.TemporaryDirectory(prefix="app-store-resolve-") as scratch:
            requirements = Path(scratch) / "requirements.in"
            requirements.write_text(requirement + "\n")
            completed = self._run(
                [
                    "uv",
                    "pip",
                    "compile",
                    str(requirements),
                    "--python-version",
                    python,
                    "--python-platform",
                    python_platform(),
                    "--no-header",
                    "--no-annotate",
                    "--quiet",
                ],
                Path(scratch),
            )
        return parse_compiled_requirements(completed)

    def resolve_npm(self, package_spec: str) -> list[tuple[str, str]]:
        with tempfile.TemporaryDirectory(prefix="app-store-resolve-") as scratch:
            folder = Path(scratch)
            (folder / "package.json").write_text('{"name": "app-store-resolve", "private": true}\n')
            self._run(
                [
                    "npm",
                    "install",
                    "--package-lock-only",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--loglevel=error",
                    package_spec,
                ],
                folder,
            )
            try:
                lock = json.loads((folder / "package-lock.json").read_text())
            except (OSError, ValueError) as e:
                raise PackageLookupError(f"npm wrote no readable lockfile: {e}") from e
        return parse_npm_lockfile(lock)

    def _run(self, argv: list[str], cwd: Path) -> str:
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as e:
            raise PackageLookupError(f"resolving took longer than {self.timeout_seconds:.0f} seconds") from e
        except OSError as e:
            raise PackageLookupError(f"could not run {argv[0]}: {e}") from e
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            raise PackageLookupError(f"resolving failed: {detail[-1] if detail else 'no output'}")
        return completed.stdout


@pure
def parse_compiled_requirements(text: str) -> list[tuple[str, str]]:
    """The ``name==version`` lines of ``uv pip compile`` output."""
    pinned: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].split(";", 1)[0].strip()
        if "==" in stripped:
            name, version = stripped.split("==", 1)
            pinned.append((name.split("[", 1)[0].strip(), version.strip()))
    return pinned


@pure
def parse_npm_lockfile(lock: dict[str, Any]) -> list[tuple[str, str]]:
    """Every installed (name, version) in a lockfile v2/v3's ``packages`` (the root entry left out)."""
    pinned: list[tuple[str, str]] = []
    packages = lock.get("packages", {})
    for path, entry in packages.items() if isinstance(packages, dict) else []:
        if path == "" or not isinstance(entry, dict) or "version" not in entry:
            continue
        name = entry.get("name") or path.rsplit("node_modules/", 1)[-1]
        pinned.append((str(name), str(entry["version"])))
    return pinned


def target_tags(python: str) -> list[Tag]:
    """The wheel tags uv accepts for CPython ``python`` on this machine, most preferred first."""
    major, minor = (int(part) for part in python.split("."))
    platforms = list(platform_tags())
    return [
        *cpython_tags(python_version=(major, minor), platforms=platforms),
        *compatible_tags(python_version=(major, minor), platforms=platforms),
    ]


@pure
def choose_distribution(files: list[dict[str, Any]], tags: list[Tag]) -> dict[str, Any] | None:
    """The file uv would install from a release's files: the wheel whose best tag ranks highest in ``tags``, else the
    source archive; None when there is neither."""
    rank = {tag: index for index, tag in enumerate(tags)}
    best: tuple[int, dict[str, Any]] | None = None
    for file in files:
        filename = str(file.get("filename", ""))
        if not filename.endswith(".whl") or file.get("yanked"):
            continue
        try:
            _name, _version, _build, wheel_tags = parse_wheel_filename(filename)
        except InvalidWheelFilename:
            continue
        positions = [rank[tag] for tag in wheel_tags if tag in rank]
        if positions and (best is None or min(positions) < best[0]):
            best = (min(positions), file)
    if best is not None:
        return best[1]
    return next((file for file in files if file.get("packagetype") == "sdist" and not file.get("yanked")), None)


def _entry_time(entry: object) -> float:
    """When a cached entry was written; an entry of the wrong shape counts as the oldest."""
    if isinstance(entry, dict) and isinstance(entry.get("at"), int | float):
        return float(entry["at"])
    return 0.0


class LookupCache(MutableModel):
    """Lookups kept on disk between checks. Thread-safe; a cache that cannot be read or written is ignored."""

    path: Path = Field(frozen=True, description="The package_lookups.json file")
    max_entries: int = Field(default=LOOKUP_CACHE_MAX_ENTRIES, frozen=True, description="Entries kept; oldest go")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _entries: dict[str, Any] | None = PrivateAttr(default=None)

    def get(self, key: str, max_age_seconds: float | None = None) -> Any:
        with self._lock:
            entry = self._loaded().get(key)
        if not isinstance(entry, dict) or "value" not in entry:
            return None
        if max_age_seconds is not None and time.time() - float(entry.get("at", 0)) > max_age_seconds:
            return None
        return entry["value"]

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            entries = self._loaded()
            entries[key] = {"value": value, "at": time.time()}
            if len(entries) > self.max_entries:
                by_age = sorted(entries, key=lambda name: _entry_time(entries[name]))
                for name in by_age[: len(entries) - self.max_entries]:
                    del entries[name]
            try:
                write_json_atomic(self.path, entries)
            except StateFileError as e:
                logger.warning("Could not cache package lookups: {}", e)

    def _loaded(self) -> dict[str, Any]:
        if self._entries is None:
            self._entries = read_json_object(self.path) or {}
        return self._entries


class PackageChecker(MutableModel):
    """Pins and estimates packages a user wants to add."""

    registry: RegistryClientInterface = Field(frozen=True, description="Reads the registries")
    resolver: ResolverInterface = Field(frozen=True, description="Resolves a package's full set")
    cache: LookupCache = Field(frozen=True, description="Lookups kept between checks")
    budget_seconds: float = Field(default=ESTIMATE_BUDGET_SECONDS, frozen=True, description="Estimate time limit")

    # Pinning

    def pin(self, request: CheckedRequest) -> PackageFacts:
        """The version that will be pinned and what the registry says about it; raises AddAppRefusedError when the
        package or version does not exist, and PackageLookupError when the registry cannot be reached."""
        key = f"pin:{request.source}:{request.package}:{request.version or 'latest'}"
        # An exact version's facts never change; what "latest" means does, so it is kept for an hour.
        cached = self.cache.get(key, None if request.version else LATEST_FRESH_SECONDS)
        if isinstance(cached, dict):
            return PackageFacts.model_validate(cached)
        if request.source == "pypi":
            facts = self._pin_pypi(request.package, request.version)
        else:
            facts = self._pin_npm(request.package, request.version)
        self.cache.put(key, facts.model_dump(mode="json"))
        return facts

    def _pin_pypi(self, name: str, version: str) -> PackageFacts:
        project = self.registry.get_json(f"{PYPI_ROOT}/pypi/{name}/json")
        if project is None:
            raise AddAppRefusedError(f"There's no package called {name!r} on PyPI.")
        info = project.get("info", {}) or {}
        releases = project.get("releases", {}) or {}
        latest = str(info.get("version", ""))
        pinned = version or latest
        if pinned not in releases and Version(pinned) not in {_safe_version(known) for known in releases}:
            raise AddAppRefusedError(f"{name} has no version {pinned} on PyPI. Its latest is {latest}.")
        if version and version != latest:
            release = self.registry.get_json(f"{PYPI_ROOT}/pypi/{name}/{pinned}/json") or {}
            info = release.get("info", info) or info
        canonical = str(info.get("name") or name)
        registry_url = f"{PYPI_ROOT}/project/{canonical}/{pinned}/"
        project_urls = info.get("project_urls") or {}
        homepage = next(
            (
                str(url)
                for key, url in project_urls.items()
                if key.lower() in ("homepage", "home", "source", "repository") and str(url).startswith("https://")
            ),
            registry_url,
        )
        license_text = str(info.get("license_expression") or info.get("license") or "").strip().splitlines()
        return PackageFacts(
            version=pinned,
            summary=str(info.get("summary") or "").strip()[:200],
            author=str(info.get("author") or info.get("maintainer") or "").strip()[:80],
            license=license_text[0][:60] if license_text else "",
            homepage_url=homepage,
            registry_url=registry_url,
        )

    def _pin_npm(self, name: str, version: str) -> PackageFacts:
        packument = self.registry.get_json(f"{NPM_REGISTRY_ROOT}/{name}")
        if packument is None:
            raise AddAppRefusedError(f"There's no package called {name!r} on npm.")
        latest = str((packument.get("dist-tags") or {}).get("latest", ""))
        versions = packument.get("versions") or {}
        pinned = version or latest
        if pinned not in versions:
            raise AddAppRefusedError(f"{name} has no version {pinned} on npm. Its latest is {latest}.")
        manifest = versions[pinned] or {}
        author = manifest.get("author")
        author_name = author.get("name", "") if isinstance(author, dict) else str(author or "")
        registry_url = f"https://www.npmjs.com/package/{name}/v/{pinned}"
        homepage = str(manifest.get("homepage") or "")
        return PackageFacts(
            version=pinned,
            summary=str(manifest.get("description") or "").strip()[:200],
            author=author_name.strip()[:80],
            license=str(manifest.get("license") or "").strip()[:60],
            homepage_url=homepage if homepage.startswith("https://") else registry_url,
            registry_url=registry_url,
        )

    # Estimating

    def estimate(self, request: CheckedRequest, version: str) -> DiskEstimate:
        """The disk estimate for the pinned package; never raises: a failure is an unknown size with its reason."""
        deadline = time.monotonic() + self.budget_seconds
        try:
            if request.source == "pypi":
                return self._estimate_pypi(request.package, version, request.python or "3.12", deadline)
            return self._estimate_npm(request.package, version, deadline)
        except PackageLookupError as e:
            logger.info("Could not estimate {}: {}", request.package, e)
            return DiskEstimate(disk_mb=None, package_count=None, basis=f"Size unknown: {e}.")

    def _estimate_pypi(self, name: str, version: str, python: str, deadline: float) -> DiskEstimate:
        requirement = f"{name}=={version}"
        resolved = self._resolved(f"pypi-resolve:{requirement}:{python}:{python_platform()}", deadline)
        if resolved is None:
            resolved = self.resolver.resolve_python(requirement, python)
            self.cache.put(f"pypi-resolve:{requirement}:{python}:{python_platform()}", resolved)
        tags = target_tags(python)
        sizes = self._lookup_all(
            [(package, pinned) for package, pinned in resolved],
            lambda package, pinned: self._pypi_file_size(package, pinned, python, tags),
            deadline,
        )
        known = [size for size in sizes if size is not None]
        missing = len(sizes) - len(known)
        downloaded_mb = sum(known) / _BYTES_PER_MB
        disk_mb = max(1, round(downloaded_mb * PYTHON_UNPACKED_FACTOR))
        basis = (
            f"Resolved with uv for Python {python} on this machine: {packages_phrase(len(resolved))}. Their files on "
            f"PyPI (the wheel uv would pick, else the source archive) come to {size_phrase(sum(known))} compressed; "
            "installed "
            f"packages take about {PYTHON_UNPACKED_FACTOR}x their download (measured on this workspace's installs), "
            f"so about {disk_mb} MB."
        )
        if missing:
            basis += f" {missing} of them had no file to measure and are not counted."
        return DiskEstimate(disk_mb=disk_mb, package_count=len(resolved), basis=basis)

    def _estimate_npm(self, name: str, version: str, deadline: float) -> DiskEstimate:
        spec = f"{name}@{version}"
        resolved = self._resolved(f"npm-resolve:{spec}", deadline)
        if resolved is None:
            resolved = self.resolver.resolve_npm(spec)
            self.cache.put(f"npm-resolve:{spec}", resolved)
        sizes = self._lookup_all(resolved, self._npm_unpacked_size, deadline)
        known = [size for size in sizes if size is not None]
        missing = len(sizes) - len(known)
        disk_mb = max(1, round(sum(known) / _BYTES_PER_MB))
        basis = (
            f"Resolved with npm: {packages_phrase(len(resolved))}. The registry gives each one's unpacked size, which "
            f"adds up to {size_phrase(sum(known))}, so about {disk_mb} MB."
        )
        if missing:
            basis += f" {missing} of them give no size and are not counted."
        return DiskEstimate(disk_mb=disk_mb, package_count=len(resolved), basis=basis)

    def _resolved(self, key: str, deadline: float) -> list[tuple[str, str]] | None:
        if time.monotonic() > deadline:
            raise PackageLookupError("the lookups took too long")
        cached = self.cache.get(key)
        if not isinstance(cached, list):
            return None
        return [(str(name), str(version)) for name, version in cached]

    def _lookup_all(self, packages: list[tuple[str, str]], lookup: Any, deadline: float) -> list[int | None]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PackageLookupError("the lookups took too long")
        with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
            futures = [pool.submit(lookup, name, version) for name, version in packages]
            results: list[int | None] = []
            for future in futures:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise PackageLookupError("the size lookups took too long")
                results.append(future.result(timeout=timeout))
        return results

    def _pypi_file_size(self, name: str, version: str, python: str, tags: list[Tag]) -> int | None:
        key = f"pypi-file:{canonicalize_name(name)}=={version}:{python}:{python_platform()}"
        cached = self.cache.get(key)
        if isinstance(cached, int):
            return cached
        release = self.registry.get_json(f"{PYPI_ROOT}/pypi/{name}/{version}/json")
        if release is None:
            return None
        chosen = choose_distribution(list(release.get("urls") or []), tags)
        size = int(chosen["size"]) if chosen is not None and isinstance(chosen.get("size"), int) else None
        if size is not None:
            self.cache.put(key, size)
        return size

    def _npm_unpacked_size(self, name: str, version: str) -> int | None:
        key = f"npm-size:{name}@{version}"
        cached = self.cache.get(key)
        if isinstance(cached, int):
            return cached
        manifest = self.registry.get_json(f"{NPM_REGISTRY_ROOT}/{name}/{version}")
        size = (manifest or {}).get("dist", {}).get("unpackedSize")
        if isinstance(size, int):
            self.cache.put(key, size)
            return size
        return None


@pure
def packages_phrase(count: int) -> str:
    return "1 package" if count == 1 else f"{count} packages"


@pure
def size_phrase(size_bytes: int) -> str:
    """A download size in plain words: KB under a megabyte, MB to one decimal above."""
    if size_bytes < _BYTES_PER_MB:
        return f"{max(1, round(size_bytes / 1024))} KB"
    return f"{size_bytes / _BYTES_PER_MB:.1f} MB"


def _safe_version(text: str) -> Version | None:
    try:
        return Version(text)
    except InvalidVersion:
        return None


def registry_name(source: str) -> str:
    return SOURCE_NAMES.get(source, source)
