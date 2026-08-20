"""The box: a full Minds computer, running as a MODAL SANDBOX -- no local Docker anywhere.

Every box is a desktop: the real Minds Electron app on a virtual display (Xvfb), streamed to the
browser via noVNC through Modal's encrypted tunnel -- you get one https://...modal.host URL, usable
from any machine, and that's the entire networking story. The image is built from docker/Dockerfile
ON MODAL'S BUILDERS (cached per mngr SHA); your machine only makes API calls. `launch` execs the
create flow INSIDE the sandbox (the CLI discovers the app's API port from in there), so the same
computer that creates a batch is the one you watch it in, and `visit-batch` finds it again by tag.

Each box is scoped to ONE Modal env via MNGR__PROVIDERS__MODAL__USER_ID (the batch name), so its
discovery only ever sees that batch's workspaces. The mngr profile's Modal SSH keypair lives on a
shared modal.Volume mounted into every box, so any box can open any workspace. Boxes auto-die at
BOX_TIMEOUT_HOURS (they bill while alive); `minds-evals stop <name>` kills one early.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from imbue.mngr_minds_eval import s3_store

# parents[2] of this file = apps/mngr_minds_eval (the image build context).
APP_DIR = Path(__file__).resolve().parents[2]
MNGR_REPO = "https://github.com/imbue-ai/mngr-internal.git"
MODAL_CONFIG = Path.home() / ".modal.toml"

# All box sandboxes live under one Modal app (in the token's default env); the batch scoping is the
# MNGR__PROVIDERS__MODAL__USER_ID env each box gets, not where the box itself lives.
APP_NAME = "minds-eval-boxes"
# One shared Modal SSH keypair for the pinned mngr profile, on a persistent Volume mounted into
# every box: the first box seeds it, later boxes reuse it -> any box can open any workspace.
MNGR_PROFILE = "evaluator"
PROFILE_VOLUME = "minds-eval-modal-profile"
PROFILE_MOUNT = "/root/.minds-staging/mngr/profiles/{}/providers/modal".format(MNGR_PROFILE)

BOX_MEMORY_MB = 16384
BOX_CPUS = 6
# Boxes bill while alive; they self-terminate after this long (visit-batch just makes a new one).
BOX_TIMEOUT_HOURS = 8
NOVNC_PORT = 6080


class BoxError(RuntimeError):
    pass


class ModalEnvExistsError(BoxError):
    """The batch's Modal env already exists -- the eval name was used before."""


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def _sh(*args: str) -> None:
    """Run a host git command, raising BoxError with stderr on failure -- plain git, so it uses your
    own credentials and a genuine auth/network problem surfaces as-is for you to debug."""
    result = _run(list(args))
    if result.returncode != 0:
        raise BoxError("`{}` failed: {}".format(" ".join(args[:3]), (result.stderr or "").strip()[:300]))


def remote_tip(branch: str) -> str:
    """The branch's current tip SHA on the mngr remote, via plain `git ls-remote` -- git uses your own
    credentials (mngr-internal is private), and a real auth/network failure surfaces as-is."""
    try:
        result = _run(["git", "ls-remote", MNGR_REPO, "refs/heads/{}".format(branch)], timeout=30)
    except subprocess.TimeoutExpired:
        raise BoxError("timed out reaching the mngr remote {} -- check your network/VPN".format(MNGR_REPO)) from None
    if result.returncode != 0:
        # A failed ls-remote (offline, auth, DNS) is NOT a missing branch -- surface the real reason.
        detail = (result.stderr or "").strip() or "git ls-remote failed"
        raise BoxError(
            "could not reach the mngr remote {} -- check your network + git auth ({})".format(MNGR_REPO, detail[:200])
        )
    ref = (result.stdout or "").split("\t")[0].strip()
    if not ref:
        raise BoxError("mngr branch {!r} not found on the remote".format(branch))
    return ref


def _fetch_mngr_source(ref: str, dest: Path) -> None:
    """A FRESH shallow clone of the exact mngr ref into `dest`, via plain git (your own credentials).
    Pulled straight from the remote into a throwaway dir -- independent of any on-device checkout, so
    your local working-tree state never reaches the box."""
    _sh("git", "init", "-q", str(dest))
    _sh("git", "-C", str(dest), "fetch", "--depth", "1", MNGR_REPO, ref)
    _sh("git", "-C", str(dest), "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")


def _stage_app_overlay(dest: Path) -> None:
    """Copy the eval-app files the image overlays onto mngr (this plugin's own version)."""
    shutil.copytree(APP_DIR / "imbue", dest / "imbue", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dest / "docker").mkdir(parents=True)
    shutil.copy2(APP_DIR / "docker" / "entrypoint.sh", dest / "docker" / "entrypoint.sh")
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(APP_DIR / name, dest / name)


def sanitize_user_id(text: str) -> str:
    """A batch id -> a Modal user_id (lowercase alnum + dashes, bounded length). The Modal env is
    named minds-<minds_env>-<user_id>, and Modal env names are restrictive."""
    slug = "".join(c if c.isalnum() else "-" for c in text.lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")[:40].rstrip("-")
    if not slug:
        raise BoxError("cannot derive a Modal user id from {!r}".format(text))
    return slug


def modal_env_name(user_id: str, minds_env: str = "staging") -> str:
    return "minds-{}-{}".format(minds_env, user_id)


def create_modal_env(user_id: str, minds_env: str = "staging") -> str:
    """Create the batch's Modal env explicitly, as an ATOMIC claim on the eval name: `modal
    environment create` fails if the env exists, which is the uniqueness preflight. Pre-creating it
    also lets every workspace create fan out concurrently. Returns the env name. TERM=dumb because
    modal 1.4.x bleeds ANSI codes into piped output."""

    env_name = modal_env_name(user_id, minds_env)
    child_env = {**os.environ, "TERM": "dumb"}
    result = _run(
        ["uv", "run", "modal", "environment", "create", env_name], cwd=str(APP_DIR.parents[1]), env=child_env
    )
    if result.returncode != 0:
        detail = ((result.stderr or "") + (result.stdout or "")).strip()
        if "already exists" in detail.lower():
            raise ModalEnvExistsError(
                "Modal env {} already exists -- eval names are unique; pick a new name (or delete it: "
                "TERM=dumb uv run python scripts/modal_nuke.py -e {} --force && "
                "TERM=dumb uv run modal environment delete {})".format(env_name, env_name, env_name)
            )
        raise BoxError("could not create Modal env {}: {}".format(env_name, detail[:300]))
    return env_name


def _modal_token_env() -> dict[str, str]:
    """MODAL_TOKEN_ID/SECRET from ~/.modal.toml (the active profile), for the box's own mngr to
    create workspaces on Modal from inside the sandbox."""
    if not MODAL_CONFIG.is_file():
        raise BoxError("missing ~/.modal.toml (Modal auth) -- everything runs on Modal")
    profiles = tomllib.loads(MODAL_CONFIG.read_text())
    active = None
    for profile in profiles.values():
        if isinstance(profile, dict) and profile.get("token_id"):
            if profile.get("active") or active is None:
                active = profile
    if not active:
        raise BoxError("no token in ~/.modal.toml -- run `modal token new`")
    return {"MODAL_TOKEN_ID": str(active["token_id"]), "MODAL_TOKEN_SECRET": str(active["token_secret"])}


def _extra_sandbox_env(
    anthropic_key: str, box_env: dict[str, str] | None, workspace_env: dict[str, str] | None
) -> dict[str, str]:
    """Overrides layered onto the box sandbox env: the anthropic key (so the in-box minds backend has
    it and forwards it to each workspace agent), any --box-env vars for the box itself, and any
    --workspace-env vars plus a MINDS_EXTRA_PASS_HOST_ENV manifest naming them (which the in-box minds
    turns into `--pass-host-env` on each create)."""
    extra: dict[str, str] = {}
    if anthropic_key:
        extra["ANTHROPIC_API_KEY"] = anthropic_key
    extra.update(box_env or {})
    forwarded = workspace_env or {}
    extra.update(forwarded)
    if forwarded:
        extra["MINDS_EXTRA_PASS_HOST_ENV"] = " ".join(sorted(forwarded))
    return extra


def _box_env(
    user_id: str,
    ref: str,
    minds_env: str,
    *,
    anthropic_key: str = "",
    box_env: dict[str, str] | None = None,
    workspace_env: dict[str, str] | None = None,
) -> dict[str, str | None]:
    """The box's sandbox env, which the in-box minds backend inherits: its Modal scope, identity, the
    Modal token (for creating workspaces from inside), and the R2 creds (load_aws_env falls back to
    env vars in-box, so no file is needed).

    ANTHROPIC_API_KEY lands HERE (the sandbox env), not just the exec'd CLI, so the in-box minds
    backend has it and the template's pass_host_env forwards it to each workspace agent. box_env are
    extra vars for the box's own mngr/minds; workspace_env are forwarded into each created workspace
    (recorded in MINDS_EXTRA_PASS_HOST_ENV, which the in-box minds turns into `--pass-host-env`)."""
    env: dict[str, str | None] = {
        "MINDS_ENV": minds_env,
        "MNGR__PROVIDERS__MODAL__USER_ID": user_id,
        "MINDS_BOX_MNGR_REF": ref,
        "MINDS_EVAL_IN_BOX": "1",
        # Every workspace this box creates is an eval worker: stack the modal_eval overlay
        # (shorter 3h sandbox timeout) on the modal template. minds' create reads this.
        "MINDS_MODAL_EXTRA_TEMPLATE": "modal_eval",
    }
    env.update(_modal_token_env())
    aws = s3_store.load_aws_env()
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
        "MINDS_EVAL_BUCKET",
        "MINDS_EVAL_S3_ENDPOINT",
    ):
        if aws.get(key):
            env[key] = aws[key]
    env.update(_extra_sandbox_env(anthropic_key, box_env, workspace_env))
    return env


def _plugin_tree_hash() -> str:
    """A stable hash of everything the image COPYs from the plugin (the overlaid CLI + entrypoint),
    embedded in the dockerfile so plugin changes always produce a new image."""
    digest = hashlib.sha256()
    files = sorted(
        list((APP_DIR / "imbue").rglob("*.py"))
        + [APP_DIR / "docker" / "entrypoint.sh", APP_DIR / "pyproject.toml", APP_DIR / "README.md"]
    )
    for path in files:
        if path.is_file() and "__pycache__" not in str(path):
            digest.update(str(path.relative_to(APP_DIR)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _tags(user_id: str, ref: str) -> dict[str, str]:
    # The plugin hash is part of a box's identity (like the image): a box whose baked-in CLI
    # predates the current plugin must not be reused, or exec'd commands hit an old parser.
    return {"minds-eval-box": user_id, "mngr-ref": ref[:12], "plugin": _plugin_tree_hash()[:8]}


def _modal():
    # Deliberately lazy: the modal SDK import is heavy and S3-only commands never need it.
    import modal

    return modal


def _app():
    return _modal().App.lookup(APP_NAME, create_if_missing=True)


def find_box(user_id: str, ref: str = ""):
    """The running box sandbox for this batch (and mngr ref, when given), or None."""
    tags = {"minds-eval-box": user_id}
    if ref:
        tags["mngr-ref"] = ref[:12]
    for sandbox in _modal().Sandbox.list(app_id=_app().app_id, tags=tags):
        return sandbox
    return None


def ensure(
    mngr_branch: str,
    *,
    user_id: str,
    ref: str = "",
    minds_env: str = "staging",
    anthropic_key: str = "",
    box_env: dict[str, str] | None = None,
    workspace_env: dict[str, str] | None = None,
):
    """Build (on Modal) + boot the box sandbox for (mngr ref, user_id); return the modal.Sandbox.

    ref defaults to the branch's current remote tip. Boxes are tagged with (user_id, ref), so if one
    is already running it is exactly the right computer -- reuse it. anthropic_key / box_env /
    workspace_env are forwarded into the sandbox env (see _box_env)."""
    modal = _modal()
    ref = ref or remote_tip(mngr_branch)
    existing = find_box(user_id, ref)
    if existing is not None:
        print(">> reusing box {} @ mngr {}".format(existing.object_id, ref[:12]), flush=True)
        return existing

    print(
        ">> booting box for {} from mngr {}@{} (image builds on Modal; first time takes minutes)".format(
            user_id, mngr_branch, ref[:12]
        ),
        flush=True,
    )
    # Assemble an EPHEMERAL build context in a throwaway temp dir: a FRESH shallow clone of the exact
    # remote ref (NOT any on-device checkout -- your working tree never touches the box) plus the
    # eval-app overlay from this plugin. The Dockerfile COPYs both, so the Modal build performs no
    # clone and needs no GitHub credentials. The temp dir (and everything in it) is removed on exit;
    # the whole build + launch runs inside the block because Modal reads the context at build time.
    # Bake ref + plugin hash into the Dockerfile text so Modal's image cache keys on them explicitly
    # (the copied mngr source varies per ref too, but this makes the cache boundary unambiguous).
    with tempfile.TemporaryDirectory(prefix="minds-box-ctx-") as ctx_dir:
        ctx = Path(ctx_dir)
        _fetch_mngr_source(ref, ctx / "mngr")
        _stage_app_overlay(ctx / "app")
        dockerfile = "# mngr {}@{} plugin-tree {}\n{}".format(
            mngr_branch, ref, _plugin_tree_hash(), (APP_DIR / "docker" / "Dockerfile").read_text()
        )
        (ctx / "Dockerfile").write_text(dockerfile)
        image = modal.Image.from_dockerfile(path=str(ctx / "Dockerfile"), context_dir=str(ctx))
        volume = modal.Volume.from_name(PROFILE_VOLUME, create_if_missing=True)
        sandbox = modal.Sandbox.create(
            "/usr/local/bin/entrypoint.sh",
            app=_app(),
            image=image,
            cpu=BOX_CPUS,
            memory=BOX_MEMORY_MB,
            timeout=BOX_TIMEOUT_HOURS * 3600,
            encrypted_ports=[NOVNC_PORT],
            env=_box_env(
                user_id,
                ref,
                minds_env,
                anthropic_key=anthropic_key,
                box_env=box_env,
                workspace_env=workspace_env,
            ),
            volumes={PROFILE_MOUNT: volume},
            tags=_tags(user_id, ref),
        )
        _await_ready(sandbox)
        return sandbox


def novnc_url(sandbox) -> str:
    """The box's desktop URL (noVNC through Modal's encrypted tunnel; reachable from anywhere)."""
    tunnel = sandbox.tunnels(timeout=120)[NOVNC_PORT]
    return "{}/vnc.html?autoconnect=true&resize=scale".format(tunnel.url.rstrip("/"))


def _await_ready(sandbox, tries: int = 100) -> None:
    """Poll until the box serves the noVNC page through its tunnel."""
    url = novnc_url(sandbox)
    print(">> waiting for the desktop at {} ...".format(url.split("/vnc.html")[0]), flush=True)
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=5)
            return
        except urllib.error.HTTPError:
            # Any HTTP response (even an error status) means it is serving.
            return
        except (urllib.error.URLError, OSError):
            pass
        if sandbox.poll() is not None:
            raise BoxError("box exited early -- see: modal sandbox logs {}".format(sandbox.object_id))
        time.sleep(3)
    raise BoxError("the box did not come up -- see: modal sandbox logs {}".format(sandbox.object_id))


def write_file(sandbox, path: str, content: str) -> None:
    with sandbox.open(path, "w") as handle:
        handle.write(content)


def run_in_box(sandbox, argv: list[str], extra_env: dict[str, str] | None = None) -> int:
    """Re-run this same CLI inside the box sandbox, streaming its output; return the exit code."""
    command = "cd /work/mngr && uv run --package mngr-minds-eval minds-evals " + " ".join(
        "'{}'".format(a.replace("'", "'\\''")) for a in argv
    )
    # Belt-and-braces: assert in-box identity explicitly rather than relying on exec inheriting
    # the sandbox's create-time env.
    env = {"MINDS_EVAL_IN_BOX": "1", **(extra_env or {})}
    process = sandbox.exec("bash", "-lc", command, env=env)
    for line in process.stdout:
        print(line, end="", flush=True)
    return process.wait()
