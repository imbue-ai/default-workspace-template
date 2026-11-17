"""Boot a preview: read the app's manifest and turn its ``[preview]`` table into a boot.

Apart from :mod:`update_app_skill.preview_app` because only ``up`` (and ``revive``, which
re-runs it) reads a manifest: ``refresh`` and ``down`` work from the preview's recorded
state, so they do not load the manifest library (and the pydantic it builds on) at startup.
"""

from __future__ import annotations

import json
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from agents_shared import serve_isolated_instance
from app_manifest.manifest import (
    MANIFEST_FILENAME,
    PREVIEW_KEY_PLACEHOLDER,
    AppManifest,
    load_manifest,
)
from app_registration import forward_port
from workspace_bare_scripts import user_packages

from update_app_skill.preview_app import (
    INNER_SERVICE_SUFFIX,
    Instances,
    InstanceSpec,
    PreviewError,
    PreviewWindow,
    _instance_locks,
    _instance_state_path,
    _preview_state_path,
    _read_json,
    _recorded_siblings,
    _reframe_previews_naming,
    _registry_copy_path,
    instance_name,
    live_preview_is_surfaced,
    live_preview_url,
    live_preview_worktree,
    live_registry_path,
    write_registry_copy,
)

# The placeholder this script fills before the shared substrate sees the table.
REGISTRY_PLACEHOLDER = "{registry}"

# The live app runs from the repo root's venv; a worktree's app runs from the worktree root
# through the worktree's own venv, which is what makes the preview serve the worktree's
# code rather than the live app's. Like a supervisord program, it syncs that environment
# --frozen and then execs the app from it, so no uv process stays resident as its parent.
LAUNCHER = (
    "bash",
    "-c",
    'uv sync --all-packages --frozen && export VIRTUAL_ENV="$PWD/.venv" PATH="$PWD/.venv/bin:$PATH" && exec "$@"',
    "bash",
)

# An app that declares ``[[secrets]]`` runs under the wrapper, which is the only reader
# of its secret files; the preview does the same. The worktree has no ``data/``, so the
# wrapper reads the live repo's files.
# own-process: wrapper -- the only reader of the app's secret files; it execs the app with them
WITH_SECRETS_SCRIPT = Path("system") / "scripts" / "with_secrets.py"
SECRETS_DIRECTORY = Path("data") / ".secrets"


def _is_same_worktree(recorded: Path, worktree: Path) -> bool:
    return recorded.resolve() == worktree.resolve()


def find_manifest(worktree: Path, app_name: str) -> tuple[Path, AppManifest]:
    """The manifest under ``worktree/system/apps/*/`` whose name is ``app_name``."""
    apps_dir = worktree / "system" / "apps"
    if not apps_dir.is_dir():
        raise PreviewError(
            f"{apps_dir} is not a directory; is --worktree a workspace checkout?"
        )
    for manifest_path in sorted(apps_dir.glob(f"*/{MANIFEST_FILENAME}")):
        manifest = load_manifest(manifest_path)
        if manifest.name == app_name:
            return manifest_path, manifest
    raise PreviewError(f"no app named {app_name!r} has a manifest under {apps_dir}")


def resolve_own_placeholders(text: str, registry_copy: Path | None) -> str:
    """Fill the placeholders only this script knows; the shared substrate fills the rest."""
    if REGISTRY_PLACEHOLDER in text:
        if registry_copy is None:
            raise PreviewError(
                f"{REGISTRY_PLACEHOLDER} is used, but no registry copy was made"
            )
        text = text.replace(REGISTRY_PLACEHOLDER, str(registry_copy))
    return text


def resolve_open_path(manifest: AppManifest, instance_key: str | None) -> str:
    if manifest.preview.open_path_takes_key:
        if not instance_key:
            raise PreviewError(
                f"a preview of {manifest.name!r} opens on an instance ({manifest.preview.open_path}); "
                "pass --instance-key"
            )
        return manifest.preview.open_path.replace(PREVIEW_KEY_PLACEHOLDER, instance_key)
    return manifest.preview.open_path


def _under_declared_secrets(
    manifest: AppManifest, repo_root: Path, launch: list[str]
) -> list[str]:
    """``launch`` run under ``with_secrets.py`` once per declared secret file, first declared outermost."""
    for secret in reversed(manifest.secrets):
        env_file = repo_root / SECRETS_DIRECTORY / f"{secret.file}.env"
        launch = [
            "python3",
            str(repo_root / WITH_SECRETS_SCRIPT),
            str(env_file),
            "--",
            *launch,
        ]
    return launch


def _declared_theming_mode(worktree: Path, manifest: AppManifest) -> str | None:
    """The ``[theming]`` mode the worktree's own app.toml declares, or None when it declares no ``[theming]``."""
    manifest_path, _ = find_manifest(worktree, str(manifest.name))
    try:
        raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    theming = raw.get("theming")
    if not isinstance(theming, dict):
        return None
    mode = theming.get("mode", "tokens")
    return mode if isinstance(mode, str) else None


def _standalone_project(worktree: Path, manifest: AppManifest) -> str | None:
    """The app's directory, relative to ``worktree``, when it is a user-built app (a standalone
    project that boots from its own venv); None for a built-in, which boots from the root venv."""
    manifest_path, _ = find_manifest(worktree, str(manifest.name))
    relative = manifest_path.parent.relative_to(worktree).as_posix()
    return relative if relative in user_packages.user_packages(worktree) else None


def build_instance_spec(
    manifest: AppManifest,
    worktree: Path,
    live_root: Path,
    window: PreviewWindow | None,
    registry_copy: Path | None,
    inner_path: str,
) -> InstanceSpec:
    """The shared substrate's boot of the manifest's preview table."""
    preview = manifest.preview
    try:
        port_env_by_name = serve_isolated_instance.parse_port_envs(
            (), [str(port_name) for port_name in preview.ports]
        )
    except serve_isolated_instance.InstanceError as exc:
        raise PreviewError(f"the [preview] table of {manifest.name!r}: {exc}") from exc
    command = list(preview.command) if preview.command else [str(manifest.program)]
    launch = _under_declared_secrets(manifest, live_root, [*command, *preview.args])
    project = _standalone_project(worktree, manifest)
    return InstanceSpec(
        app=str(manifest.name),
        name=instance_name(manifest.name),
        command=[
            *(LAUNCHER if project is None else ()),
            *(resolve_own_placeholders(part, registry_copy) for part in launch),
        ],
        cwd=worktree,
        port_env_by_name=port_env_by_name,
        env_overrides={
            key: resolve_own_placeholders(value, registry_copy)
            for key, value in preview.env.items()
        },
        copy_sources={str(key): source for key, source in preview.copies.items()},
        health_path=preview.health_path,
        window=window,
        inner_path=inner_path,
        theming_mode=_declared_theming_mode(worktree, manifest),
        project=project,
    )


def _live_root(
    repo_root: Path, worktree: Path, *, is_private: bool, with_apps: Sequence[str]
) -> Path:
    """The live checkout a preview from ``repo_root`` runs over; raises PreviewError for a
    preview that would register a window nobody could open."""
    main_checkout = serve_isolated_instance.main_checkout_of_linked_worktree(repo_root)
    if main_checkout is not None and not is_private:
        raise PreviewError(
            f"{repo_root} is a git worktree of {main_checkout}, so a preview booted from here "
            "would register its window where the workspace never looks, and the user could "
            f"not open it. To show it to the user, run from {main_checkout} with --worktree "
            f"{worktree}; for an instance only you reach on its loopback port, pass --private."
        )
    if is_private and with_apps:
        raise PreviewError(
            "a preview frames each --with sibling at the origin its window registers, and a "
            "private instance registers none. Drop --with to boot the app alone, or drop "
            "--private to preview it with a window from the live checkout."
        )
    return main_checkout or repo_root


def up(
    app_name: str,
    worktree: Path,
    repo_root: Path,
    *,
    with_apps: Sequence[str] = (),
    instance_key: str | None = None,
    title: str | None = None,
    is_private: bool = False,
    instances: Instances,
    dump_registry: Callable[[list[dict[str, object]]], str] | None = None,
) -> int:
    """Boot the app's preview from ``worktree``, after the siblings named in ``with_apps``.

    ``is_private`` boots it with no registrations, title, or instance key, for an agent
    to reach on its loopback port. Raises PreviewError for a preview with a window from a
    ``repo_root`` that is a linked git worktree, and for ``is_private`` with ``with_apps``.
    Holds the lock of every instance it may boot, so a ``revive`` asked for meanwhile waits.
    """
    with _instance_locks(
        repo_root, [app_name, *with_apps, *_recorded_siblings(repo_root, app_name)]
    ):
        return _up_locked(
            app_name,
            worktree,
            repo_root,
            with_apps=with_apps,
            instance_key=instance_key,
            title=title,
            is_private=is_private,
            instances=instances,
            dump_registry=dump_registry,
        )


def _up_locked(
    app_name: str,
    worktree: Path,
    repo_root: Path,
    *,
    with_apps: Sequence[str],
    instance_key: str | None,
    title: str | None,
    is_private: bool,
    instances: Instances,
    dump_registry: Callable[[list[dict[str, object]]], str] | None,
) -> int:
    """``up``, with the locks of the app and its siblings already held."""
    live_root = _live_root(
        repo_root, worktree, is_private=is_private, with_apps=with_apps
    )
    other = live_preview_worktree(repo_root, app_name)
    if other is not None and not _is_same_worktree(other, worktree):
        sys.stderr.write(
            f"preview: another pass's preview of {app_name!r} is already up, serving {other}; the "
            f"'{instance_name(app_name)}' app can only be one at a time, so booting this one would "
            "hijack it. Surface this to the user and coordinate with that pass -- or, if it is "
            f"abandoned, tear it down first with 'down --app {app_name}'.\n"
        )
        return 1
    if is_private and live_preview_is_surfaced(repo_root, app_name):
        sys.stderr.write(
            f"preview: a preview of {app_name!r} with a window is already up, and a private "
            "instance would replace it, closing the user's window on it. Drop --private to "
            f"re-boot it with its window, or tear it down first with 'down --app {app_name}'.\n"
        )
        return 1
    manifest_path, manifest = find_manifest(worktree, app_name)
    dump = dump_registry if dump_registry is not None else forward_port.dump_registry
    # Siblings first, because the registry copy this app is booted with has to name their
    # URLs.
    preview_url_by_app: dict[str, str] = {}
    for sibling in with_apps:
        # A sibling opens on the same instance when its path takes one (the chat under a shell
        # preview opens on the user's conversation, like a chat preview of its own).
        if (
            _up_locked(
                sibling,
                worktree,
                repo_root,
                with_apps=(),
                instance_key=instance_key,
                title=None,
                is_private=False,
                instances=instances,
                dump_registry=dump,
            )
            != 0
        ):
            return 1
        sibling_url = live_preview_url(repo_root, sibling)
        if sibling_url is None:
            sys.stderr.write(
                f"preview: the preview of {sibling!r} came up but recorded no port.\n"
            )
            return 1
        preview_url_by_app[sibling] = sibling_url
    registry_copy: Path | None = None
    if _uses_placeholder(manifest, REGISTRY_PLACEHOLDER):
        registry_copy = _registry_copy_path(repo_root, app_name)
        write_registry_copy(
            live_registry_path(repo_root),
            registry_copy,
            preview_url_by_app,
            dump,
        )
    # A private instance has no frame to open on a page, so it needs no instance key.
    if is_private:
        window = None
        inner_path = "/"
    else:
        window = PreviewWindow(
            service_name=f"{manifest.name}{INNER_SERVICE_SUFFIX}",
            preview_service_name=instance_name(manifest.name),
            title=title if title is not None else f"{manifest.display_name} (preview)",
        )
        inner_path = resolve_open_path(manifest, instance_key)
    spec = build_instance_spec(
        manifest,
        worktree,
        live_root,
        window,
        registry_copy,
        inner_path,
    )
    # An earlier ``up --with`` of this preview may have booted siblings this call does not
    # name; they are still running, and ``down`` finds them only through this record.
    siblings = list(
        dict.fromkeys([*with_apps, *_recorded_siblings(repo_root, app_name)])
    )
    _preview_state_path(repo_root, app_name).parent.mkdir(parents=True, exist_ok=True)
    _preview_state_path(repo_root, app_name).write_text(
        json.dumps(
            {
                "app": app_name,
                "worktree": str(worktree),
                "with": siblings,
                "manifest": str(manifest_path),
                # With the worktree and siblings, what ``revive`` boots it again with.
                "instance_key": instance_key,
                "title": title,
                "is_private": is_private,
            }
        )
    )
    code = instances.up(spec, repo_root)
    if (
        code != 0
        and not siblings
        and not _instance_state_path(repo_root, app_name).exists()
    ):
        # Kept when siblings may be up: their names live only in this record, so dropping it
        # would leave them running with nothing left for ``down`` to reach them by. They are
        # left running rather than torn down here, since the usual next move is a rebuild and
        # a retry, which reuses them. Kept too when the failed boot kept the rows an earlier
        # boot registered, which an open window shows, so ``revive`` can try again.
        _preview_state_path(repo_root, app_name).unlink(missing_ok=True)
    if code == 0:
        _reframe_previews_naming(repo_root, app_name, dump)
    return code


def _uses_placeholder(manifest: AppManifest, placeholder: str) -> bool:
    preview = manifest.preview
    texts = [*preview.command, *preview.args, *preview.env.values()]
    return any(placeholder in text for text in texts)


def revive(app_name: str, repo_root: Path, *, instances: Instances) -> int:
    """Boot the app's preview again as its last ``up`` booted it, with the siblings it frames.

    What a workspace restart leaves of a preview is its record and the rows its window
    shows; this re-runs that ``up``, which keeps those rows, so an open window shows the
    preview again where it was, over a fresh copy of the live data. A preview whose
    processes all still run is left alone, and two revives at once boot it once.
    Returns 1, saying why on stderr, when no preview of the app is recorded or the boot
    fails (a build that no longer boots). Raises PreviewError when the recorded worktree
    is gone or no longer holds the app's manifest.
    """
    siblings = _recorded_siblings(repo_root, app_name)
    # Every instance this boots, its siblings' included, so a sibling's own revive waits for
    # this one.
    with _instance_locks(repo_root, [app_name, *siblings]):
        record = _read_json(_preview_state_path(repo_root, app_name))
        if record is None:
            sys.stderr.write(
                f"preview: no preview of {app_name!r} is recorded, so there is nothing to "
                "start again; boot one with 'up'.\n"
            )
            return 1
        if all(
            instances.is_running(instance_name(name), repo_root)
            for name in [app_name, *siblings]
        ):
            sys.stderr.write(
                f"preview: the preview of {app_name!r} is already running.\n"
            )
            return 0
        title = record.get("title")
        instance_key = record.get("instance_key")
        return _up_locked(
            app_name,
            Path(str(record["worktree"])),
            repo_root,
            with_apps=siblings,
            instance_key=str(instance_key) if instance_key is not None else None,
            title=str(title) if title is not None else None,
            is_private=bool(record.get("is_private")),
            instances=instances,
            dump_registry=None,
        )
