"""The rules for an added app's request, and the file the added apps are kept in."""

import json
from pathlib import Path

import pytest

from app_store.added_apps import ADDED_APPS_FORMAT
from app_store.added_apps import AddAppRequest
from app_store.added_apps import AddedApp
from app_store.added_apps import AddedAppStore
from app_store.added_apps import check_command
from app_store.added_apps import check_request
from app_store.added_apps import slug_from_display_name
from app_store.added_apps import taken_app_names
from app_store.catalog import CatalogApp
from app_store.catalog import LaunchCommand
from app_store.catalog import PythonRecipe
from app_store.catalog import ResourceEstimate
from app_store.catalog import render_argv
from app_store.errors import AddAppRefusedError

_STREAMLIT = "streamlit hello --server.port {port} --server.address 127.0.0.1 --server.headless true"


def _pypi(package: str = "streamlit", **overrides: str) -> AddAppRequest:
    return AddAppRequest.model_validate({"source": "pypi", "package": package, "command": _STREAMLIT, **overrides})


def _npm(package: str = "http-server", **overrides: str) -> AddAppRequest:
    fields = {"source": "npm", "package": package, "command": "http-server {data_dir} -p {port} -a 127.0.0.1"}
    return AddAppRequest.model_validate({**fields, **overrides})


def test_a_python_request_is_split_once_and_runs_from_the_apps_environment() -> None:
    checked = check_request(_pypi(version="==1.40.0", python="3.11"), taken_names=set())
    assert (checked.package, checked.version, checked.title, checked.slug, checked.python) == (
        "streamlit",
        "1.40.0",
        "streamlit",
        "streamlit",
        "3.11",
    )
    assert checked.command == (
        "streamlit",
        "hello",
        "--server.port",
        "{port}",
        "--server.address",
        "127.0.0.1",
        "--server.headless",
        "true",
    )
    assert checked.launch_argv[0] == "{env_dir}/bin/streamlit"


def test_an_npm_request_finds_its_program_in_node_modules_and_names_itself_from_the_display_name() -> None:
    checked = check_request(_npm(version="v14.1.1"), taken_names=set())
    assert (checked.version, checked.title, checked.slug, checked.python) == (
        "14.1.1",
        "http-server",
        "httpserver",
        None,
    )
    assert checked.launch_argv[0] == "{env_dir}/node_modules/.bin/http-server"
    scoped = check_request(_npm("@scope/tool-x", display_name="My Tool 2"), taken_names=set())
    assert (scoped.package, scoped.slug) == ("@scope/tool-x", "mytool2")


def test_quotes_group_words_and_nothing_else_is_special() -> None:
    checked = check_request(_pypi(command='streamlit run "my app.py" --port {port} --host 127.0.0.1 $HOME;ls'), set())
    assert checked.arguments == ("run", "my app.py", "--port", "{port}", "--host", "127.0.0.1", "$HOME;ls")


@pytest.mark.parametrize(
    ("package", "complaint"),
    [
        ("git+https://github.com/excalidraw/excalidraw", "Links aren't supported"),
        ("https://example.com/pkg.tar.gz", "Links aren't supported"),
        ("file:../thing", "Links aren't supported"),
        ("./local/thing", "file path"),
        ("thing-1.0.tar.gz", "package file"),
        ("thing-1.0-py3-none-any.whl", "package file"),
        ("streamlit[all]", "Extras aren't supported"),
        ("pkg @ https://example.com/x.zip", "Links aren't supported"),
        ("pkg@1.0", "Direct references"),
        ("streamlit>=1.0", "exact version in the version field"),
        ("-bad-", "isn't a valid Python package name"),
        ("", "Enter the package's name"),
    ],
)
def test_only_a_plain_pypi_name_is_accepted(package: str, complaint: str) -> None:
    with pytest.raises(AddAppRefusedError, match=complaint):
        check_request(_pypi(package), set())


@pytest.mark.parametrize("version", [">=1.0", "~=1.2", "1.*", "1.0,<2", "^1.0", "!=1"])
def test_a_python_version_range_is_refused(version: str) -> None:
    with pytest.raises(AddAppRefusedError, match="is a range"):
        check_request(_pypi(version=version), set())


@pytest.mark.parametrize(("version", "complaint"), [("banana", "isn't a valid"), ("1.0+local", "local version")])
def test_a_python_version_that_is_not_an_exact_release_is_refused(version: str, complaint: str) -> None:
    with pytest.raises(AddAppRefusedError, match=complaint):
        check_request(_pypi(version=version), set())


@pytest.mark.parametrize(
    ("package", "complaint"),
    [
        ("github:excalidraw/excalidraw", "Links aren't supported"),
        ("git+ssh://git@github.com/x/y.git", "Links aren't supported"),
        ("http-server-14.1.1.tgz", "package file"),
        ("/tmp/http-server", "file path"),
        ("http-server@14.1.1", "version field"),
        ("Http-Server", "isn't a valid npm package name"),
        ("_private", "isn't a valid npm package name"),
    ],
)
def test_only_a_plain_npm_name_is_accepted(package: str, complaint: str) -> None:
    with pytest.raises(AddAppRefusedError, match=complaint):
        check_request(_npm(package), set())


@pytest.mark.parametrize("version", ["^14.0.0", "~14.1.0", "14", "14.x", ">=14.0.0", "latest"])
def test_an_npm_version_must_be_exact(version: str) -> None:
    with pytest.raises(AddAppRefusedError, match="isn't one exact version"):
        check_request(_npm(version=version), set())


@pytest.mark.parametrize(
    ("command", "complaint"),
    [
        ("", "Enter a start command"),
        ('streamlit run "unclosed', "can't be read"),
        ("/usr/bin/streamlit hello {port} 127.0.0.1", "bare name"),
        ("{env_dir}/bin/streamlit {port} 127.0.0.1", "bare name"),
        ("streamlit --dir {env_dir} --port {port} 127.0.0.1", "only {port} and {data_dir}"),
        ("streamlit --json {x --port {port} 127.0.0.1", "stray brace"),
        ("streamlit hello --server.address 127.0.0.1", "must pass the app its port as {port}"),
        ("streamlit hello --server.port {port}", "listen on 127.0.0.1"),
    ],
)
def test_a_start_command_breaking_a_rule_is_refused(command: str, complaint: str) -> None:
    with pytest.raises(AddAppRefusedError, match=complaint):
        check_request(_pypi(command=command), set())


@pytest.mark.parametrize(
    ("display_name", "taken", "complaint"),
    [
        ("Files", {"files"}, "something already has that name"),
        ("Share", set(), "something already has that name"),
        ("123", set(), "doesn't make a usable name"),
        ("!!", set(), "doesn't make a usable name"),
        ("x" * 65, set(), "64 characters"),
    ],
)
def test_a_name_that_is_taken_reserved_or_unusable_is_refused(
    display_name: str, taken: set[str], complaint: str
) -> None:
    with pytest.raises(AddAppRefusedError, match=complaint):
        check_request(_pypi(display_name=display_name), taken)


def test_only_the_offered_python_versions_are_accepted() -> None:
    with pytest.raises(AddAppRefusedError, match="isn't offered"):
        check_request(_pypi(python="2.7"), set())


def test_a_display_name_gives_a_slug_by_the_catalogs_rules() -> None:
    assert [slug_from_display_name(name) for name in ("My Notes!", "3D Viewer", "http-server", "Ünïcode Ok")] == [
        "mynotes",
        "dviewer",
        "httpserver",
        "ncodeok",
    ]


def test_taken_names_cover_the_store_the_workspace_and_its_programs(tmp_path: Path) -> None:
    (tmp_path / "chat.conf").write_text("")
    (tmp_path / "terminal-pty.conf").write_text("")
    assert taken_app_names({"jupyterlab"}, {"files"}, tmp_path) == {"jupyterlab", "files", "chat", "terminal-pty"}
    assert taken_app_names(set(), set(), tmp_path / "missing") == set()


def _added(slug: str) -> AddedApp:
    return AddedApp(
        app=CatalogApp(
            slug=slug,
            title=slug.title(),
            description="Demo.",
            about="Demo.",
            author="Someone",
            license="MIT",
            homepage_url="https://pypi.org/project/demo/",
            source_url="https://pypi.org/project/demo/1.0/",
            version="1.0",
            icon="added.svg",
            recipe=PythonRecipe(kind="python", requirement="demo==1.0"),
            launch=LaunchCommand(argv=("{env_dir}/bin/demo", "--port", "{port}", "127.0.0.1")),
            resources=ResourceEstimate(disk_mb=None),
        ),
        source="pypi",
        package="demo",
        command=("demo", "--port", "{port}", "127.0.0.1"),
        package_count=None,
        registry_url="https://pypi.org/project/demo/1.0/",
        added_at="2026-11-05T00:00:00+00:00",
    )


def test_added_apps_are_kept_in_a_versioned_file_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / "added-apps.json"
    store = AddedAppStore(path=path)
    assert store.read() == ()
    store.add(_added("demo"))
    store.add(_added("other"))
    store.add(_added("demo"))
    document = json.loads(path.read_text())
    assert document["format"] == ADDED_APPS_FORMAT
    assert [entry["app"]["slug"] for entry in document["apps"]] == ["other", "demo"]
    assert [entry.app.slug for entry in AddedAppStore(path=path).read()] == ["other", "demo"]
    found = store.find("demo")
    assert found is not None and found.command[0] == "demo"
    store.remove("other")
    assert [entry.app.slug for entry in store.read()] == ["demo"]
    assert not list(tmp_path.glob("*.tmp-*"))


def test_a_malformed_entry_is_skipped_and_the_rest_still_load(tmp_path: Path) -> None:
    path = tmp_path / "added-apps.json"
    good = _added("demo").model_dump(mode="json")
    broken = {**good, "app": {**good["app"], "slug": "Bad Slug"}}
    path.write_text(json.dumps({"format": ADDED_APPS_FORMAT, "apps": [broken, good, "nonsense"]}))
    assert [entry.app.slug for entry in AddedAppStore(path=path).read()] == ["demo"]


@pytest.mark.parametrize("text", ['{"format": 99, "apps": []}', '{"apps": "no"}', "not json", "[1, 2]"])
def test_an_unreadable_file_reads_as_no_added_apps(tmp_path: Path, text: str) -> None:
    path = tmp_path / "added-apps.json"
    path.write_text(text)
    assert AddedAppStore(path=path).read() == ()


def test_a_literal_brace_is_written_twice_and_a_single_one_says_how() -> None:
    program, arguments = check_command(
        """jupyter-notebook --port={port} --ip=127.0.0.1 --tornado='{{"headers": {{"a": "b"}}}}'"""
    )
    rendered = render_argv((program, *arguments), 8090, Path("/d"), Path("/e"))
    assert rendered[-1] == '--tornado={"headers": {"a": "b"}}'
    with pytest.raises(AddAppRefusedError, match=r"brace\s+twice"):
        check_command("""jupyter-notebook --port={port} --ip=127.0.0.1 --tornado='{"headers": 1}'""")
