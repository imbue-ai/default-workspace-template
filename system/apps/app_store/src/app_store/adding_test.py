"""Adding apps end to end with the system faked: check, add, and an added app taking exactly the same install, repair,
uninstall and roll-back paths as a shipped one; then removing it from the store."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app_manifest.registry import RegistryRow
from app_store.added_apps import AddAppRequest
from app_store.added_apps import SetupFileText
from app_store.adding import AppAdder
from app_store.adding import EXCLUDE_BLOCK_END
from app_store.adding import EXCLUDE_BLOCK_START
from app_store.adding import local_exclude_path
from app_store.adding import replace_exclude_block
from app_store.engine import InstallEngine
from app_store.errors import AddAppRefusedError
from app_store.errors import InstallRefusedError
from app_store.launch import plan_launch
from app_store.package_check import PYTHON_UNPACKED_FACTOR
from app_store.records import InstallStatus
from app_store.records import JobKind
from app_store.room import WorkspaceRoom
from app_store.testing import FakePackageInstaller
from app_store.testing import FakePortProbe
from app_store.testing import FakeProcessMemoryReader
from app_store.testing import FakeRegistryClient
from app_store.testing import FakeResourceReader
from app_store.testing import FakeSupervisor
from app_store.testing import build_test_adder
from app_store.testing import build_test_engine
from app_store.testing import make_registry_row

_DEMO_COMMAND = "demo serve --port {port} --host 127.0.0.1 --root {data_dir}"


def _demo(**overrides: str) -> AddAppRequest:
    return AddAppRequest.model_validate(
        {"source": "pypi", "package": "demo", "display_name": "Demo", "command": _DEMO_COMMAND, **overrides}
    )


def _setup(tmp_path: Path, registry_rows: list[RegistryRow] | None = None) -> tuple[InstallEngine, AppAdder]:
    engine = build_test_engine(tmp_path, registry_rows=registry_rows)
    return engine, build_test_adder(engine, tmp_path)


def _installed(engine: InstallEngine) -> list[str]:
    assert isinstance(engine.installer, FakePackageInstaller)
    return engine.installer.installed


def _peak(engine: InstallEngine, slug: str) -> int | None:
    record = engine.records.read().find(slug)
    assert record is not None
    return record.peak_memory_mb


def test_a_check_pins_estimates_and_previews_without_installing(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    result = adder.check(_demo())
    assert (result.slug, result.title, result.version, result.registry) == ("demo", "Demo", "1.1", "PyPI")
    assert (result.package_count, result.disk_mb, result.verdict) == (2, round(4 * PYTHON_UNPACKED_FACTOR), "FITS")
    assert result.command_preview[0] == f"{engine.layout.env_dir('demo')}/bin/demo"
    assert result.command_preview[1:5] == ("serve", "--port", "8090", "--host")
    assert result.command_preview[-1] == str(engine.layout.app_data_dir("demo"))
    assert result.registry_url == "https://pypi.org/project/demo/1.1/"
    assert engine.added.read() == ()
    assert engine.records.read().records == ()


def test_a_check_says_when_the_disk_will_not_fit_and_add_is_refused(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    assert isinstance(engine.resources, FakeResourceReader)
    engine.resources.room = WorkspaceRoom(disk_free_mb=5, memory_available_mb=4_000)
    result = adder.check(_demo())
    assert result.verdict == "WONT_FIT"
    assert "Demo needs about 13 MB of disk" in result.blocked_reason
    with pytest.raises(InstallRefusedError):
        adder.add(_demo())
    assert engine.added.read() == ()


def test_low_memory_warns_but_never_blocks_an_added_app(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    assert isinstance(engine.resources, FakeResourceReader)
    engine.resources.room = WorkspaceRoom(disk_free_mb=30_000, memory_available_mb=50)
    assert adder.check(_demo()).verdict == "TIGHT"
    assert adder.add(_demo()).status is InstallStatus.QUEUED


def test_names_taken_by_the_store_the_workspace_or_another_added_app_are_refused(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path, registry_rows=[make_registry_row("files", 8300)])
    with pytest.raises(AddAppRefusedError, match="something already has that name"):
        adder.check(_demo(display_name="JupyterLab"))
    with pytest.raises(AddAppRefusedError, match="something already has that name"):
        adder.check(_demo(display_name="Files"))
    adder.add(_demo())
    with pytest.raises(AddAppRefusedError, match="something already has that name"):
        adder.check(_demo())


def test_an_unreachable_registry_is_a_plain_refusal(tmp_path: Path) -> None:
    _engine, adder = _setup(tmp_path)
    registry = adder.checker.registry
    assert isinstance(registry, FakeRegistryClient)
    registry.unreachable.add("https://pypi.org/pypi/demo/json")
    with pytest.raises(AddAppRefusedError, match="PyPI couldn't be reached"):
        adder.check(_demo())


def test_an_added_app_installs_repairs_and_uninstalls_like_a_shipped_one(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    installer = engine.installer
    supervisor = engine.supervisor
    assert isinstance(installer, FakePackageInstaller) and isinstance(supervisor, FakeSupervisor)

    queued = adder.add(_demo())
    assert (queued.slug, queued.status, queued.job) == ("demo", InstallStatus.QUEUED, JobKind.INSTALL)
    added = engine.added.find("demo")
    assert added is not None and added.app.version == "1.1" and added.package_count == 2
    assert "/system/supervisord.conf.d/demo.conf" in (tmp_path / "info" / "exclude").read_text()

    assert engine.run_next_job()
    record = engine.records.read().find("demo")
    assert record is not None and record.status is InstallStatus.INSTALLED
    assert record.measured_disk_mb is not None
    assert installer.installed == ["demo==1.1"]
    assert supervisor.programs == {"demo"}
    assert "app-store-launch demo" in engine.layout.dropin_path("demo").read_text()
    plan = plan_launch("demo", engine.layout)
    argv, _env = plan.render("demo-abc123")
    assert argv[0] == f"{engine.layout.env_dir('demo')}/bin/demo"
    assert argv[-1] == str(engine.layout.app_data_dir("demo"))

    # A restore that lost the environment: the repair reinstalls the same pin.
    shutil.rmtree(engine.layout.env_dir("demo"))
    engine.reconcile()
    assert engine.run_next_job()
    assert installer.installed == ["demo==1.1", "demo==1.1"]
    repaired = engine.records.read().find("demo")
    assert repaired is not None and repaired.status is InstallStatus.INSTALLED

    with pytest.raises(AddAppRefusedError, match="Uninstall it first"):
        adder.remove("demo")
    engine.request_uninstall("demo", is_deleting_data=False)
    assert engine.run_next_job()
    assert engine.records.read().find("demo") is None
    assert supervisor.programs == set()

    # Remove from store takes the entry, its excludes line and anything it kept.
    assert engine.layout.app_dir("demo").exists()
    adder.remove("demo")
    assert engine.added.read() == ()
    assert not engine.layout.app_dir("demo").exists()
    assert "demo.conf" not in (tmp_path / "info" / "exclude").read_text()


def test_an_exact_version_stays_pinned(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    adder.add(_demo(version="1.0"))
    engine.run_next_job()
    assert _installed(engine) == ["demo==1.0"]


def test_a_program_the_package_does_not_provide_fails_rolls_back_and_names_what_it_did(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    adder.add(_demo(command="demoserver --port {port} --host 127.0.0.1"))
    engine.run_next_job()

    record = engine.records.read().find("demo")
    assert record is not None and record.status is InstallStatus.FAILED
    assert record.failure_reason == (
        "The package didn't install a program called 'demoserver'. The programs it did install: demo. Change the "
        "start command to use one of them and try again."
    )
    assert not engine.layout.env_dir("demo").exists()
    assert not engine.layout.dropin_path("demo").exists()
    assert "didn't install a program called 'demoserver'" in engine.layout.log_path("demo").read_text()

    # Fixing the command means removing and adding again; nothing is left behind in between.
    adder.remove("demo")
    assert not engine.layout.app_dir("demo").exists()
    assert engine.records.read().find("demo") is None
    adder.add(_demo())
    engine.run_next_job()
    fixed = engine.records.read().find("demo")
    assert fixed is not None and fixed.status is InstallStatus.INSTALLED


def test_an_app_that_never_answers_times_out_rolls_back_and_its_output_lands_in_the_log(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    assert isinstance(engine.probe, FakePortProbe)
    engine.probe.is_answering = False
    logs = engine.program_log_directory
    logs.mkdir(parents=True)
    (logs / "demo-stdout.log").write_text(" ________\n< moo >\n --------\n")
    (logs / "demo-stderr.log").write_text("exited\n")
    adder.add(_demo())
    engine.run_next_job()

    record = engine.records.read().find("demo")
    assert record is not None and record.status is InstallStatus.FAILED
    assert "didn't start answering on its port" in record.failure_reason
    log = engine.layout.log_path("demo").read_text()
    assert "the last of demo's stdout:" in log and "< moo >" in log and "exited" in log
    assert not engine.layout.dropin_path("demo").exists()
    # Retry runs the same install again.
    engine.probe.is_answering = True
    engine.request_install("demo")
    engine.run_next_job()
    retried = engine.records.read().find("demo")
    assert retried is not None and retried.status is InstallStatus.INSTALLED


def test_an_added_npm_app_installs_its_pinned_package(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    adder.add(
        AddAppRequest(source="npm", package="demo-server", command="demo-server {data_dir} -p {port} -a 127.0.0.1")
    )
    engine.run_next_job()
    record = engine.records.read().find("demoserver")
    assert record is not None and record.status is InstallStatus.INSTALLED
    assert _installed(engine) == ["demo-server@2.0.0"]


def test_remove_refuses_an_app_that_is_not_added(tmp_path: Path) -> None:
    _engine, adder = _setup(tmp_path)
    with pytest.raises(AddAppRefusedError, match="no added app called 'jupyterlab'"):
        adder.remove("jupyterlab")


def test_a_broken_or_clashing_added_entry_never_takes_the_store_down(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    adder.add(_demo())
    clashing = engine.added.find("demo")
    assert clashing is not None
    engine.added.add(clashing.model_copy(update={"app": clashing.app.model_copy(update={"slug": "datasette"})}))
    slugs = [app.slug for app in engine.catalog.apps]
    assert slugs.count("datasette") == 1 and "demo" in slugs


def _with_a_stale_entry_named(engine: InstallEngine, slug: str) -> None:
    """Keep a copy of the added app ``demo`` renamed ``slug``, as a shipped catalog that grew would leave it."""
    demo = engine.added.find("demo")
    assert demo is not None
    engine.added.add(demo.model_copy(update={"app": demo.app.model_copy(update={"slug": slug})}))


def test_an_added_entry_a_shipped_app_renamed_neither_blocks_launches_nor_removes_the_shipped_apps_files(
    tmp_path: Path,
) -> None:
    engine, adder = _setup(tmp_path)
    adder.add(_demo())
    engine.run_next_job()
    engine.request_install("datasette")
    engine.run_next_job()
    _with_a_stale_entry_named(engine, "datasette")

    # Every installed app still launches, and the name means the shipped app.
    assert plan_launch("demo", engine.layout).argv[0] == "{env_dir}/bin/demo"
    assert plan_launch("datasette", engine.layout).argv[0] == "{env_dir}/bin/datasette"
    # Taking the stale entry out leaves the shipped app's record and files alone.
    engine.request_uninstall("datasette", is_deleting_data=False)
    engine.run_next_job()
    data_file = engine.layout.app_data_dir("datasette") / "kept.db"
    data_file.write_text("mine")
    adder.remove("datasette")
    assert data_file.read_text() == "mine"
    assert [entry.app.slug for entry in engine.added.read()] == ["demo"]


def test_a_repeated_added_entry_keeps_the_first_and_the_store_still_loads(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    adder.add(_demo())
    document = json.loads(engine.added.path.read_text())
    second = json.loads(json.dumps(document["apps"][0]))
    second["app"]["title"] = "Demo again"
    document["apps"].append(second)
    engine.added.path.write_text(json.dumps(document))

    offered = [app for app in engine.catalog.apps if app.slug == "demo"]
    assert [app.title for app in offered] == ["Demo"]
    engine.run_next_job()
    assert plan_launch("demo", engine.layout).argv[0] == "{env_dir}/bin/demo"


def test_peak_memory_is_sampled_over_the_first_minutes_of_each_run(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    adder.add(_demo())
    engine.run_next_job()
    reader = engine.memory_reader
    assert isinstance(reader, FakeProcessMemoryReader)

    engine.sample_memory_once(0.0)
    assert _peak(engine, "demo") is None
    reader.pid_by_program["demo"] = 42
    reader.memory_by_program["demo"] = 120
    engine.sample_memory_once(10.0)
    reader.memory_by_program["demo"] = 300
    engine.sample_memory_once(60.0)
    reader.memory_by_program["demo"] = 200
    engine.sample_memory_once(90.0)
    assert _peak(engine, "demo") == 300
    # Past the window of this run, nothing more is recorded...
    reader.memory_by_program["demo"] = 900
    engine.sample_memory_once(500.0)
    assert _peak(engine, "demo") == 300
    # ...until a new run starts.
    reader.pid_by_program["demo"] = 43
    engine.sample_memory_once(510.0)
    assert _peak(engine, "demo") == 900


def test_the_excludes_block_is_rewritten_and_left_out_when_empty() -> None:
    other = "# mine\n*.swp\n"
    with_block = replace_exclude_block(other, ["/a.conf"])
    assert with_block == f"# mine\n*.swp\n{EXCLUDE_BLOCK_START}\n/a.conf\n{EXCLUDE_BLOCK_END}\n"
    assert replace_exclude_block(with_block, ["/b.conf"]).count(EXCLUDE_BLOCK_START) == 1
    assert replace_exclude_block(with_block, []) == other


def test_the_excludes_file_is_found_inside_a_repository_only(tmp_path: Path) -> None:
    assert local_exclude_path(tmp_path) is None
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert local_exclude_path(tmp_path) == tmp_path / ".git" / "info" / "exclude"


def test_settings_files_are_written_before_the_first_start_and_never_replace_the_persons_own(tmp_path: Path) -> None:
    engine, adder = _setup(tmp_path)
    config = SetupFileText(path="conf/app.yaml", content="port: {port}\nroot: {data_dir}\nmap: {a: 1}\n")
    adder.add(_demo().model_copy(update={"setup_files": (config,)}))
    added = engine.added.find("demo")
    assert added is not None and added.app.launch.setup_files[0].path == "conf/app.yaml"

    assert engine.run_next_job()
    record = engine.records.read().find("demo")
    assert record is not None and record.status is InstallStatus.INSTALLED
    written = engine.layout.app_data_dir("demo") / "conf" / "app.yaml"
    data_dir = engine.layout.app_data_dir("demo")
    assert written.read_text() == f"port: {record.port}\nroot: {data_dir}\nmap: {{a: 1}}\n"
    assert "wrote conf/app.yaml" in engine.layout.log_path("demo").read_text()

    # The person edits it; a repair keeps their edit.
    written.write_text("mine\n")
    shutil.rmtree(engine.layout.env_dir("demo"))
    engine.reconcile()
    assert engine.run_next_job()
    assert written.read_text() == "mine\n"


@pytest.mark.parametrize(
    ("files", "complaint"),
    [
        ((SetupFileText(path="/etc/passwd", content=""),), "plain path inside the app's own folder"),
        ((SetupFileText(path="../escape.yaml", content=""),), "plain path inside the app's own folder"),
        ((SetupFileText(path="plugin/index.js", content=""),), "never code"),
        ((SetupFileText(path="run.sh", content=""),), "never code"),
        ((SetupFileText(path="a.yaml", content=""), SetupFileText(path="a.yaml", content="")), "same name"),
        ((SetupFileText(path="big.json", content="x" * 20_001),), "too long"),
        (tuple(SetupFileText(path=f"f{index}.json", content="") for index in range(6)), "at most 5"),
    ],
)
def test_settings_files_that_break_a_rule_are_refused_in_plain_words(
    tmp_path: Path, files: tuple[SetupFileText, ...], complaint: str
) -> None:
    _engine, adder = _setup(tmp_path)
    with pytest.raises(AddAppRefusedError, match=complaint):
        adder.check(_demo().model_copy(update={"setup_files": files}))


def test_a_check_shows_the_settings_files_it_will_write(tmp_path: Path) -> None:
    _engine, adder = _setup(tmp_path)
    config = SetupFileText(path="settings.toml", content="a = 1\n")
    result = adder.check(_demo().model_copy(update={"setup_files": (config,)}))
    assert [(file.path, file.content) for file in result.setup_files] == [("settings.toml", "a = 1\n")]
