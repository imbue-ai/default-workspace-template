"""Pinning and estimating packages against faked registries and resolvers."""

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from packaging.tags import Tag
from pydantic import Field
from pydantic import PrivateAttr

from app_store.added_apps import CheckedRequest
from app_store.errors import AddAppRefusedError
from app_store.errors import PackageLookupError
from app_store.package_check import CommandResolver
from app_store.package_check import LookupCache
from app_store.package_check import PYTHON_UNPACKED_FACTOR
from app_store.package_check import PackageChecker
from app_store.package_check import RegistryClientInterface
from app_store.package_check import choose_distribution
from app_store.package_check import packages_phrase
from app_store.package_check import parse_compiled_requirements
from app_store.package_check import parse_npm_lockfile
from app_store.package_check import size_phrase
from app_store.package_check import target_tags
from app_store.testing import FakeRegistryClient
from app_store.testing import demo_checker


def _request(source: str = "pypi", package: str = "demo", version: str = "") -> CheckedRequest:
    return CheckedRequest(
        source="pypi" if source == "pypi" else "npm",
        package=package,
        version=version,
        title=package,
        slug="demo",
        program=package,
        arguments=("--port", "{port}", "127.0.0.1"),
        python="3.12" if source == "pypi" else None,
    )


def _registry(checker: PackageChecker) -> FakeRegistryClient:
    assert isinstance(checker.registry, FakeRegistryClient)
    return checker.registry


def test_an_empty_version_pins_the_latest_and_reads_the_registrys_facts(tmp_path: Path) -> None:
    facts = demo_checker(tmp_path).pin(_request())
    assert (facts.version, facts.summary, facts.author, facts.license) == ("1.1", "A demo app.", "Someone", "MIT")
    assert facts.registry_url == "https://pypi.org/project/demo/1.1/"


def test_an_exact_version_is_kept_and_must_exist(tmp_path: Path) -> None:
    checker = demo_checker(tmp_path)
    assert checker.pin(_request(version="1.0")).version == "1.0"
    with pytest.raises(AddAppRefusedError, match="demo has no version 9.9 on PyPI. Its latest is 1.1."):
        checker.pin(_request(version="9.9"))


def test_a_package_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    checker = demo_checker(tmp_path)
    with pytest.raises(AddAppRefusedError, match="There's no package called 'nope' on PyPI"):
        checker.pin(_request(package="nope"))
    with pytest.raises(AddAppRefusedError, match="There's no package called 'nope' on npm"):
        checker.pin(_request("npm", "nope"))
    with pytest.raises(AddAppRefusedError, match="has no version 1.0.0 on npm"):
        checker.pin(_request("npm", "demo-server", "1.0.0"))


def test_an_npm_package_pins_its_latest_tag(tmp_path: Path) -> None:
    facts = demo_checker(tmp_path).pin(_request("npm", "demo-server"))
    assert (facts.version, facts.summary, facts.author, facts.license) == ("2.0.0", "Serves files.", "A", "MIT")
    assert facts.registry_url == "https://www.npmjs.com/package/demo-server/v/2.0.0"


def test_pins_are_cached(tmp_path: Path) -> None:
    checker = demo_checker(tmp_path)
    checker.pin(_request(version="1.0"))
    fetched = len(_registry(checker).fetched)
    assert checker.pin(_request(version="1.0")).version == "1.0"
    assert len(_registry(checker).fetched) == fetched


def test_an_unreachable_registry_raises_a_lookup_error(tmp_path: Path) -> None:
    checker = demo_checker(tmp_path)
    _registry(checker).unreachable.add("https://pypi.org/pypi/demo/json")
    with pytest.raises(PackageLookupError):
        checker.pin(_request())


def test_a_python_estimate_sums_the_chosen_files_and_scales_them_to_installed_size(tmp_path: Path) -> None:
    estimate = demo_checker(tmp_path).estimate(_request(), "1.1")
    # demo's wheel (3 MB) and helper's (1 MB), not their 10x source archives.
    assert estimate.package_count == 2
    assert estimate.disk_mb == round(4 * PYTHON_UNPACKED_FACTOR)
    assert "2 packages" in estimate.basis
    assert "4.0 MB compressed" in estimate.basis
    assert f"{PYTHON_UNPACKED_FACTOR}x" in estimate.basis


def test_an_npm_estimate_sums_unpacked_sizes_and_says_which_are_missing(tmp_path: Path) -> None:
    estimate = demo_checker(tmp_path).estimate(_request("npm", "demo-server"), "2.0.0")
    assert (estimate.package_count, estimate.disk_mb) == (2, 5)
    assert "1 of them give no size and are not counted" in estimate.basis


def test_estimates_are_cached(tmp_path: Path) -> None:
    checker = demo_checker(tmp_path)
    checker.estimate(_request(), "1.1")
    fetched = len(_registry(checker).fetched)
    again = demo_checker(tmp_path).estimate(_request(), "1.1")
    assert again.disk_mb == round(4 * PYTHON_UNPACKED_FACTOR)
    assert len(_registry(checker).fetched) == fetched


def test_a_failed_resolution_leaves_the_size_unknown_with_the_reason(tmp_path: Path) -> None:
    estimate = demo_checker(tmp_path).estimate(_request(), "7.0")
    assert (estimate.disk_mb, estimate.package_count) == (None, None)
    assert estimate.basis == "Size unknown: resolving failed: no solution for demo==7.0."


def test_running_out_of_time_leaves_the_size_unknown(tmp_path: Path) -> None:
    checker = demo_checker(tmp_path)
    slow = PackageChecker(registry=checker.registry, resolver=checker.resolver, cache=checker.cache, budget_seconds=0)
    estimate = slow.estimate(_request(), "1.1")
    assert estimate.disk_mb is None
    assert "took too long" in estimate.basis


class _HangingRegistry(RegistryClientInterface):
    """Answers like ``inner``, except that a URL in ``hanging`` waits until the test lets it go."""

    inner: FakeRegistryClient = Field(description="The registry it answers like")
    hanging: set[str] = Field(description="URLs that do not answer until released")

    _released: threading.Event = PrivateAttr(default_factory=threading.Event)

    def get_json(self, url: str) -> dict[str, Any] | None:
        if url in self.hanging:
            self._released.wait(30)
        return self.inner.get_json(url)

    def release(self) -> None:
        self._released.set()


def test_a_size_lookup_that_hangs_leaves_the_size_unknown_on_time(tmp_path: Path) -> None:
    checker = demo_checker(tmp_path)
    registry = _HangingRegistry(inner=_registry(checker), hanging={"https://pypi.org/pypi/helper/2.0/json"})
    hanging = PackageChecker(registry=registry, resolver=checker.resolver, cache=checker.cache, budget_seconds=0.5)
    started = time.monotonic()
    try:
        estimate = hanging.estimate(_request(), "1.1")
    finally:
        registry.release()
    assert time.monotonic() - started < 5
    assert (estimate.disk_mb, estimate.package_count) == (None, None)
    assert estimate.basis == "Size unknown: the size lookups took too long."


def test_a_package_with_no_release_to_pin_is_refused_and_a_missing_size_is_not_counted(tmp_path: Path) -> None:
    checker = demo_checker(tmp_path)
    registry = _registry(checker)
    registry.documents["https://registry.npmjs.org/unreleased"] = {"dist-tags": {}, "versions": {}}
    with pytest.raises(AddAppRefusedError, match="unreleased has no release on npm to pin"):
        checker.pin(_request("npm", "unreleased"))
    registry.documents["https://pypi.org/pypi/unreleased/json"] = {"info": {"version": None}, "releases": {}}
    with pytest.raises(AddAppRefusedError, match="unreleased has no release on PyPI to pin"):
        checker.pin(_request(package="unreleased"))
    registry.documents["https://registry.npmjs.org/mime/1.6.0"] = {"dist": None}
    estimate = checker.estimate(_request("npm", "demo-server"), "2.0.0")
    assert (estimate.package_count, estimate.disk_mb) == (2, 5)


def test_the_file_uv_would_choose_is_the_best_matching_wheel_else_the_source_archive() -> None:
    tags = [Tag("cp312", "cp312", "manylinux_2_17_x86_64"), Tag("py3", "none", "any")]
    files = [
        {"filename": "x-1.0-cp312-cp312-win_amd64.whl", "size": 1},
        {"filename": "x-1.0-py3-none-any.whl", "size": 2},
        {"filename": "x-1.0-cp312-cp312-manylinux_2_17_x86_64.whl", "size": 3},
        {"filename": "x-1.0.tar.gz", "packagetype": "sdist", "size": 4},
        {"filename": "broken.whl", "size": 5},
    ]
    assert choose_distribution(files, tags) == files[2]
    assert choose_distribution(files[:2], tags) == files[1]
    assert choose_distribution([files[0], files[3]], tags) == files[3]
    assert choose_distribution([{**files[2], "yanked": True}, files[0]], tags) is None


def test_the_target_tags_put_cpython_wheels_before_pure_python_ones() -> None:
    tags = [str(tag) for tag in target_tags("3.12")]
    assert tags.index("py3-none-any") > next(index for index, tag in enumerate(tags) if tag.startswith("cp312-cp312-"))


def test_resolver_output_is_parsed_into_pins() -> None:
    compiled = "altair==5.5.0\n    # via streamlit\nnumpy==2.3.4 ; python_version >= '3.11'\nrich[jupyter]==14.0\n"
    assert parse_compiled_requirements(compiled) == [("altair", "5.5.0"), ("numpy", "2.3.4"), ("rich", "14.0")]
    lock = {
        "packages": {
            "": {"name": "root"},
            "node_modules/http-server": {"version": "14.1.1"},
            "node_modules/a/node_modules/@s/b": {"version": "1.0.0"},
            "node_modules/linked": {"link": True},
        }
    }
    assert parse_npm_lockfile(lock) == [("http-server", "14.1.1"), ("@s/b", "1.0.0")]


def test_the_command_resolver_reports_a_failure_as_a_lookup_error(tmp_path: Path) -> None:
    with pytest.raises(PackageLookupError, match="resolving failed"):
        CommandResolver(timeout_seconds=60).resolve_npm("this-package-cannot-exist-zz9@0.0.1")


def test_the_lookup_cache_expires_what_it_is_told_to(tmp_path: Path) -> None:
    cache = LookupCache(path=tmp_path / "lookups.json")
    cache.put("a", 1)
    assert cache.get("a") == 1
    assert cache.get("a", max_age_seconds=-1) is None
    assert LookupCache(path=tmp_path / "lookups.json").get("a") == 1
    assert cache.get("missing") is None


def test_the_lookup_cache_keeps_only_its_newest_entries(tmp_path: Path) -> None:
    path = tmp_path / "lookups.json"
    path.write_text(
        json.dumps({"oldest": {"value": 0, "at": 1.0}, "odd": "not an entry", "old": {"value": 1, "at": 2.0}})
    )
    cache = LookupCache(path=path, max_entries=3)
    cache.put("new", 2)
    cache.put("newer", 3)
    assert sorted(json.loads(path.read_text())) == ["new", "newer", "old"]
    assert (cache.get("oldest"), cache.get("old"), cache.get("newer")) == (None, 1, 3)


def test_counts_and_sizes_read_naturally() -> None:
    assert [packages_phrase(count) for count in (1, 2)] == ["1 package", "2 packages"]
    assert [size_phrase(size) for size in (100, 40 * 1024, 3 * 1024 * 1024)] == ["1 KB", "40 KB", "3.0 MB"]
