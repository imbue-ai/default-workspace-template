"""Orchestrator for the ``minds_deployment`` + ``minds_services`` test suites.

Plain-Python (click-driven) entrypoint -- NOT a pytest wrapper. Owns:

* The DEFAULT_WORKSPACE_TEMPLATE worktree at ``<monorepo>/.external_worktrees/default-workspace-template/``:
  validation, stash + push to a ``ci-<timestamp>`` branch on the DEFAULT_WORKSPACE_TEMPLATE
  remote, and stash-restore so the operator's worktree state is
  unchanged.
* The per-run mail.tm account: creation via the public mail.tm HTTP
  API, env-var threading into pytest, and deletion in cleanup.
* Shared CI env stand-up via ``minds-admin env deploy`` (subprocess), serial
  for the initial single-``default``-env roster.
* Sequential dispatch of the two pytest invocations
  (``-m minds_deployment`` first, then ``-m minds_services``).
* Per-run ledger at ``.minds/ci-test-deploys.jsonl``: append-on-create,
  walked for end-of-run teardown, paired cleanup mode for prior runs.
* Name + age sweep: enumerates ``ci-*`` Modal envs and destroys
  anything older than 4 hours.

Wired up to satisfy the spec's command surface. The env lifecycle --
the ``minds-admin env deploy`` / ``destroy`` shellouts, the per-run secret
handoff, the fixed CI test-user creation, and the name+age sweep -- is
implemented. The DEFAULT_WORKSPACE_TEMPLATE branch push/delete steps remain explicitly stubbed
for Phase 2 and log a clear "not implemented yet" warning rather than
silently no-op-ing.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never
from uuid import uuid4

import click
import httpx
import psycopg2
from loguru import logger
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.config.loader import load_client_config
from imbue.minds.deployment_tests.data_types import DefaultWorkspaceTemplateRef
from imbue.minds.deployment_tests.data_types import DeploymentEnvsConfig
from imbue.minds.deployment_tests.data_types import PoolProvisionInfo
from imbue.minds.deployment_tests.data_types import SharedEnvUrls
from imbue.minds.deployment_tests.helpers import POOL_DWT_READ_KEY_SECRET_KEY
from imbue.minds.deployment_tests.helpers import build_minds_env_subprocess_env
from imbue.minds.deployment_tests.helpers import create_verified_user_via_admin_api
from imbue.minds.deployment_tests.helpers import delete_pool_secrets
from imbue.minds.deployment_tests.helpers import delete_shared_env_secrets
from imbue.minds.deployment_tests.helpers import publish_pool_secrets
from imbue.minds.deployment_tests.helpers import publish_shared_env_secrets
from imbue.minds.deployment_tests.helpers import read_ci_test_user_credentials
from imbue.minds.deployment_tests.helpers import read_shared_env_secrets
from imbue.minds.deployment_tests.primitives import DEPLOYMENT_ENVS_JSON_ENV_VAR
from imbue.minds.deployment_tests.primitives import MAILTM_ADDRESS_ENV_VAR
from imbue.minds.deployment_tests.primitives import MAILTM_JWT_ENV_VAR
from imbue.minds.deployment_tests.primitives import RunId
from imbue.minds.deployment_tests.primitives import SHARED_ENV_SECRET_ENV_VAR_PREFIX
from imbue.minds.deployment_tests.primitives import SharedEnvRole
from imbue.minds.envs.paths import client_config_file
from imbue.minds.envs.paths import env_root_dir
from imbue.minds.envs.paths import secrets_file
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import admin_key_from_supertokens_secret
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.errors import MindError
from imbue.minds.utils.output import write_stdout_line
from imbue.minds_admin.cli._activated_env import MODAL_PROFILE_ENV_VAR
from imbue.minds_admin.cli._activated_env import modal_profile_for_tier_or_none
from imbue.minds_admin.cli._activated_env import tier_for_env_name
from imbue.minds_admin.envs.local_store import read_secrets_file
from imbue.minds_admin.envs.local_store import write_secrets_file
from imbue.minds_admin.envs.r2_cleanup import CloudflareR2Credentials
from imbue.minds_admin.envs.r2_cleanup import R2CleanupError
from imbue.minds_admin.envs.r2_cleanup import SuperTokensCoreCredentials
from imbue.minds_admin.envs.r2_cleanup import sweep_orphaned_r2_buckets
from imbue.minds_admin.slices.bare_metal_db import fetch_servers
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_imbue_cloud.primitives import OVH_DATACENTER_CODE_BY_US_REGION
from imbue.mngr_imbue_cloud.repo_identity import canonicalize_repo_source
from imbue.mngr_imbue_cloud.slices.bare_metal import find_first_ready_server_in_datacenter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_DEFAULT_WORKSPACE_TEMPLATE_WORKTREE_PATH: Final[Path] = (
    _REPO_ROOT / ".external_worktrees" / "default-workspace-template"
)
_DEFAULT_WORKSPACE_TEMPLATE_REMOTE_URL: Final[str] = "git@github.com:imbue-ai/default-workspace-template.git"
_LEDGER_PATH: Final[Path] = _REPO_ROOT / ".minds" / "ci-test-deploys.jsonl"
_DEPLOYMENT_ENVS_JSON_PATH: Final[Path] = _REPO_ROOT / "test-results" / "deployment_envs.json"
_ITERATE_STATE_DIR: Final[Path] = _REPO_ROOT / ".minds"
_DEFAULT_MAX_RESOURCE_AGE_HOURS: Final[int] = 4

_MAILTM_API_BASE: Final[str] = "https://api.mail.tm"

# Default shared-env roster. The spec's initial roster is a single ``default``
# env; expansion is a matter of editing this tuple (and registering more
# roles in tests that need them via ``shared_env('<role>')``).
_DEFAULT_SHARED_ENV_ROLES: Final[tuple[SharedEnvRole, ...]] = (SharedEnvRole("default"),)

_MINDS_DEPLOY_TIMEOUT_SECONDS: Final[int] = 15 * 60
_MINDS_DESTROY_TIMEOUT_SECONDS: Final[int] = 10 * 60
# The bake's long pole is one cold workspace-image build (seed phase) plus the
# per-slice carve + docker load fan-out; warm-content runs finish far sooner.
_MINDS_BAKE_TIMEOUT_SECONDS: Final[int] = 90 * 60
# The warm verb is at most one cold seed build (one throwaway slice, bounded
# retries); nothing in the workflow waits on it, so the cap only bounds runner
# spend when it wedges.
_MINDS_WARM_TIMEOUT_SECONDS: Final[int] = 45 * 60
# Cap on how many slices the CI bake fan-out runs at once. The measured warm
# path (carve + docker load + bootstrap) is minutes per slice, so baking the
# whole default roster concurrently avoids a second wave; capped so a large
# --count re-run cannot over-contend the box.
_MAX_CI_BAKE_CONCURRENCY: Final[int] = 8
_MINDS_SWEEP_TIMEOUT_SECONDS: Final[int] = 15 * 60
_GIT_TIMEOUT_SECONDS: Final[int] = 600
# Remote-workspace release test roster (lease/isolation, fast-path create,
# stop/start) plus two spares for flaky retries and post-run debugging.
_DEFAULT_CI_POOL_SLICE_COUNT: Final[int] = 5
_DEFAULT_CI_POOL_REGION: Final[str] = "US-EAST-VA"
_DEFAULT_WORKSPACE_TEMPLATE_HTTPS_URL: Final[str] = "https://github.com/imbue-ai/default-workspace-template.git"
# Base64 private key (read-only deploy key on the template repo) the CI bake
# stage clones with; local operators rely on ambient git credentials instead.
_DWT_READ_KEY_ENV_VAR: Final[str] = "MINDS_CI_DWT_KEY_B64"
# Global pytest-session deadline for the deployment/services suites. They each
# stand up real cloud envs and legitimately run for many minutes (the three
# minds_deployment tests together are ~10-12 min), far beyond the default.
_PYTEST_MAX_DURATION_SECONDS: Final[int] = 60 * 60
_MODAL_ENV_LIST_TIMEOUT_SECONDS: Final[int] = 60
# Used only to resolve the ci tier's Modal workspace when listing envs for the
# sweep; never materialized as a real env.
_CI_TIER_PROBE_ENV_NAME: Final[str] = "ci-probe"


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class LedgerKind(UpperCaseStrEnum):
    """What kind of resource a ledger entry tracks."""

    ENV = auto()
    DEFAULT_WORKSPACE_TEMPLATE_BRANCH = auto()
    MAILTM_ACCOUNT = auto()


class LedgerStatus(UpperCaseStrEnum):
    """Lifecycle status of a ledger-tracked resource."""

    ACTIVE = auto()
    DESTROYED = auto()
    LEAKED = auto()


class LedgerEntry(FrozenModel):
    """One JSONL row in ``.minds/ci-test-deploys.jsonl``.

    Append-only by convention: a new entry is appended for every create
    and for every state change (we never edit prior lines in place).
    Readers fold all rows for a given ``name`` and pick the latest by
    file order to determine current status.
    """

    kind: LedgerKind
    name: NonEmptyStr = Field(description="Resource-specific identifier (env name, branch name, mail.tm account id).")
    created_at: datetime
    run_id: RunId
    status: LedgerStatus


def _append_ledger_entry(entry: LedgerEntry) -> None:
    _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = entry.model_dump_json()
    with _LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _read_ledger_entries() -> list[LedgerEntry]:
    if not _LEDGER_PATH.is_file():
        return []
    entries: list[LedgerEntry] = []
    for line_number, raw in enumerate(_LEDGER_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            entries.append(LedgerEntry.model_validate_json(stripped))
        except ValueError as exc:
            raise MindError(f"Malformed ledger line at {_LEDGER_PATH}:{line_number}: {stripped!r} ({exc})") from exc
    return entries


def _latest_status_by_name(entries: Iterable[LedgerEntry]) -> dict[NonEmptyStr, LedgerEntry]:
    """Fold entries by name; later rows win (append-only update semantics)."""
    latest: dict[NonEmptyStr, LedgerEntry] = {}
    for entry in entries:
        latest[entry.name] = entry
    return latest


def _mark_status(name: NonEmptyStr, *, kind: LedgerKind, run_id: RunId, status: LedgerStatus) -> None:
    """Append a status-change row for ``name`` (preserves the original ``created_at``)."""
    existing = _latest_status_by_name(_read_ledger_entries()).get(name)
    created_at = existing.created_at if existing is not None else datetime.now(timezone.utc)
    _append_ledger_entry(LedgerEntry(kind=kind, name=name, created_at=created_at, run_id=run_id, status=status))


def _drop_destroyed_rows_if_drained() -> None:
    """Remove ``ci-test-deploys.jsonl`` once every tracked resource is destroyed.

    Keeps the file from growing unboundedly across many runs while
    still preserving the append-only audit log within an active set.
    """
    entries = _read_ledger_entries()
    if not entries:
        if _LEDGER_PATH.is_file():
            _LEDGER_PATH.unlink()
        return
    latest = _latest_status_by_name(entries)
    if all(entry.status == LedgerStatus.DESTROYED for entry in latest.values()):
        _LEDGER_PATH.unlink()
        logger.info("Ledger drained -- removed {}", _LEDGER_PATH)


# ---------------------------------------------------------------------------
# Run id
# ---------------------------------------------------------------------------


def _mint_run_id() -> RunId:
    """Compact ISO 8601 UTC, lowercase ``t``/``z`` so it fits in ``DevEnvName``.

    Format ``YYYYMMDDtHHMMSSz`` (e.g. ``20260518t140212z``). Lex sort
    equals chronological sort.
    """
    return RunId(datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz"))


# ---------------------------------------------------------------------------
# DEFAULT_WORKSPACE_TEMPLATE worktree
# ---------------------------------------------------------------------------


class DefaultWorkspaceTemplateWorktreeMissingError(MindError):
    """Raised when ``.external_worktrees/default-workspace-template/`` is not present."""


def _validate_default_workspace_template_worktree() -> None:
    """Warn (do not fail) if the DEFAULT_WORKSPACE_TEMPLATE worktree is missing.

    No Phase 1 test creates a DEFAULT_WORKSPACE_TEMPLATE workspace -- the deleted workspace/signup
    services tests were the only consumers -- so a missing worktree is not
    fatal today (and CI runners don't have one). Phase 2 re-adds the
    workspace-creating tests and will restore the hard requirement here.
    """
    if (
        not _DEFAULT_WORKSPACE_TEMPLATE_WORKTREE_PATH.is_dir()
        or not (_DEFAULT_WORKSPACE_TEMPLATE_WORKTREE_PATH / ".git").exists()
    ):
        logger.warning(
            "DEFAULT_WORKSPACE_TEMPLATE worktree missing at {} -- continuing (no Phase 1 test needs it). To enable the "
            "future workspace/signup tests, create it with `git worktree add -B <branch> {} <branch>` "
            "from a DEFAULT_WORKSPACE_TEMPLATE clone.",
            _DEFAULT_WORKSPACE_TEMPLATE_WORKTREE_PATH,
            _DEFAULT_WORKSPACE_TEMPLATE_WORKTREE_PATH,
        )


def _push_default_workspace_template_test_branch(*, run_id: RunId) -> str:
    """Stash + commit + push the worktree's contents to ``ci-<run_id>`` on the DEFAULT_WORKSPACE_TEMPLATE remote.

    Returns the branch name. Records the branch in the ledger. The
    operator's primary DEFAULT_WORKSPACE_TEMPLATE clone is never touched.

    Stub for now: stamped out per the spec but not yet exercised by the
    tests (they all skip). The stash + push code lives here so iterating
    on it does not require touching anything else.
    """
    branch_name = f"ci-{run_id}"
    logger.warning(
        "DEFAULT_WORKSPACE_TEMPLATE branch push to {!r} is stubbed out -- the push flow is documented in the spec but "
        "not yet wired up. Tests today use the local worktree path via the default_workspace_template_ref fixture.",
        branch_name,
    )
    _append_ledger_entry(
        LedgerEntry(
            kind=LedgerKind.DEFAULT_WORKSPACE_TEMPLATE_BRANCH,
            name=NonEmptyStr(branch_name),
            created_at=datetime.now(timezone.utc),
            run_id=run_id,
            status=LedgerStatus.ACTIVE,
        )
    )
    return branch_name


def _delete_default_workspace_template_test_branch(branch_name: str, *, run_id: RunId) -> None:
    """Delete the pushed test branch from the DEFAULT_WORKSPACE_TEMPLATE remote. Idempotent against already-gone."""
    logger.warning(
        "DEFAULT_WORKSPACE_TEMPLATE branch deletion for {!r} is stubbed out -- pair with the push stub. The age-sweep "
        "will eventually be the safety net here.",
        branch_name,
    )
    _mark_status(
        NonEmptyStr(branch_name),
        kind=LedgerKind.DEFAULT_WORKSPACE_TEMPLATE_BRANCH,
        run_id=run_id,
        status=LedgerStatus.DESTROYED,
    )


# ---------------------------------------------------------------------------
# mail.tm
# ---------------------------------------------------------------------------


class _MailtmAccount(FrozenModel):
    """Per-run disposable mail.tm account.

    Holds the credentials needed for the orchestrator's own bookkeeping
    (the ``account_id`` for ledger entries + the JWT for the delete call
    at end-of-run) plus the ``address`` exported to the pytest process.
    The account password is consumed once by ``/token`` during creation
    and not retained -- every later mail.tm API call uses the JWT.
    """

    account_id: NonEmptyStr
    address: NonEmptyStr
    jwt: SecretStr


def _create_mailtm_account(*, run_id: RunId) -> _MailtmAccount:
    """Create a fresh disposable mail.tm account; return creds + record in ledger.

    The ledger entry is appended as soon as the account is created on mail.tm,
    before the JWT is minted, so a failure between account creation and token
    mint still leaves a trail for ``cleanup`` to find.
    """
    with httpx.Client(base_url=_MAILTM_API_BASE, timeout=20.0) as client:
        domains_response = client.get("/domains")
        domains_response.raise_for_status()
        domains = domains_response.json().get("hydra:member", [])
        if not domains:
            raise MindError("mail.tm returned an empty domains list; cannot create a test account.")
        domain = domains[0]["domain"]
        local_part = f"ci-{run_id}-{get_short_random_string()}"
        address = f"{local_part}@{domain}"
        password = uuid4().hex
        account_response = client.post("/accounts", json={"address": address, "password": password})
        account_response.raise_for_status()
        account_id = NonEmptyStr(account_response.json()["id"])
        # Record the account in the ledger now -- before requesting the JWT --
        # so a /token failure leaves a recoverable trail rather than orphaning
        # the account on mail.tm.
        _append_ledger_entry(
            LedgerEntry(
                kind=LedgerKind.MAILTM_ACCOUNT,
                name=account_id,
                created_at=datetime.now(timezone.utc),
                run_id=run_id,
                status=LedgerStatus.ACTIVE,
            )
        )
        token_response = client.post("/token", json={"address": address, "password": password})
        token_response.raise_for_status()
        jwt = token_response.json()["token"]
    account = _MailtmAccount(
        account_id=account_id,
        address=NonEmptyStr(address),
        jwt=SecretStr(jwt),
    )
    logger.info("Created per-run mail.tm account {}", account.address)
    return account


def _delete_mailtm_account(account_id: NonEmptyStr, jwt: SecretStr, *, run_id: RunId) -> None:
    """Delete a mail.tm account by id. Idempotent against already-gone."""
    with httpx.Client(base_url=_MAILTM_API_BASE, timeout=20.0) as client:
        response = client.delete(
            f"/accounts/{account_id}",
            headers={"Authorization": f"Bearer {jwt.get_secret_value()}"},
        )
        if response.status_code not in (204, 404):
            response.raise_for_status()
    _mark_status(account_id, kind=LedgerKind.MAILTM_ACCOUNT, run_id=run_id, status=LedgerStatus.DESTROYED)


# ---------------------------------------------------------------------------
# Shared envs
# ---------------------------------------------------------------------------


def _mint_shared_env_name(*, run_id: RunId, role: SharedEnvRole) -> DevEnvName:
    """``ci-<run-id>-<short>`` (default role), or with the role appended otherwise.

    Every CI env name MUST include both a timestamp AND a random suffix:
    the timestamp is what the name+age sweep parses to decide which envs
    are old enough to destroy (regex :data:`_CI_ENV_NAME_PATTERN`
    anchors on ``^ci-<timestamp>``), and the random suffix prevents
    name collisions between two runs that happen to start in the same
    UTC second (e.g. two concurrent orchestrator invocations, or a
    re-run within a single second of the prior one). The role -- when
    not ``default`` -- is appended LAST so the timestamp stays at
    position 2 and the sweep regex matches every shape uniformly.

    The default-role name (``ci-`` + 16-char timestamp + ``-`` + 8-hex suffix,
    28 chars) must stay within ``MAX_SLICE_ENV_NAME_LENGTH`` -- the env is
    stamped into every pool slice's lima names, whose length limactl caps (the
    pool bake fails fast past the cap).
    """
    short = get_short_random_string()
    if role == SharedEnvRole("default"):
        return DevEnvName(f"ci-{run_id}-{short}")
    return DevEnvName(f"ci-{run_id}-{short}-{role}")


def _deploy_shared_env(*, name: DevEnvName, role: SharedEnvRole) -> SharedEnvUrls:
    """Deploy a fresh ci env, create the fixed CI test user, publish per-env secrets; return URLs.

    Shells out to ``minds-admin env deploy`` with the activation env vars set
    (so it targets ``name`` without a prior ``eval activate``), parses
    the resulting ``client.toml`` for the connector + litellm URLs, reads
    the per-env secrets the deploy wrote (the freshly-minted SuperTokens
    app + Neon DSNs), creates the fixed CI test user against the new
    SuperTokens app, and publishes those per-env secrets to the env-keyed
    Vault path so the test runner + destroy/sweep jobs can read them back.
    """
    env_root_dir(name).mkdir(parents=True, exist_ok=True)
    sub_env = build_minds_env_subprocess_env(name)
    logger.info("Deploying shared env {!r} (role={!r})", name, role)
    completed = subprocess.run(
        ["uv", "run", "minds-admin", "env", "deploy"],
        env=sub_env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_MINDS_DEPLOY_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise MindError(
            f"`minds-admin env deploy` for {name!r} exited {completed.returncode}.\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    client_toml = client_config_file(name)
    if not client_toml.is_file():
        raise MindError(
            f"`minds-admin env deploy` for {name!r} completed but did not write {client_toml}. "
            "This usually means the modal-side deploy succeeded but the local-state write step failed."
        )
    client_config = load_client_config(client_toml)
    urls = SharedEnvUrls(
        role=role,
        env_name=name,
        connector_url=client_config.connector_url,
        litellm_proxy_url=client_config.litellm_proxy_url,
    )
    secrets_model = read_secrets_file(name)
    secrets = {key: value.get_secret_value() for key, value in secrets_model.secrets.items()}
    _create_ci_test_user(secrets=secrets, connector_url=str(client_config.connector_url), name=name)
    # The tier's fixed admin key rides along with the per-env secrets: the CI
    # test job's Vault role cannot read the static supertokens entry, and the
    # admin-authenticated deployment tests (the backup-retention reap) need it.
    admin_key = admin_key_from_supertokens_secret(
        read_vault_kv(VaultPath(f"{_CI_VAULT_PREFIX}/supertokens")), _CI_VAULT_PREFIX
    )
    publish_shared_env_secrets(env_name=name, role=role, secrets={**secrets, "MINDS_ADMIN_KEY": admin_key})
    logger.info("Shared env {!r} deployed; connector={}", name, urls.connector_url)
    return urls


def _create_ci_test_user(*, secrets: dict[str, str], connector_url: str, name: DevEnvName) -> None:
    """Create the fixed verified CI test user against a freshly-deployed env's SuperTokens app."""
    missing = [key for key in ("SUPERTOKENS_CONNECTION_URI", "SUPERTOKENS_API_KEY") if not secrets.get(key)]
    if missing:
        raise MindError(f"Deployed env {name!r} secrets.toml is missing {missing}; cannot create the CI test user.")
    email, password = read_ci_test_user_credentials()
    create_verified_user_via_admin_api(
        connection_uri=SecretStr(secrets["SUPERTOKENS_CONNECTION_URI"]),
        api_key=SecretStr(secrets["SUPERTOKENS_API_KEY"]),
        connector_url=AnyUrl(connector_url),
        email=email,
        password=password,
    )
    logger.info("Created CI test user {!r} on env {!r}", str(email), name)


def _destroy_env(name: DevEnvName, *, run_id: RunId) -> None:
    """Run ``uv run minds-admin env destroy`` for the named env + delete its per-env Vault secrets.

    Works cross-machine (CI's deploy and destroy run on separate runners, and
    the leaked-env sweep runs on a third). ``minds-admin env destroy`` re-derives most
    cloud resources from Vault + the env name, but its pool-slice teardown step
    auto-resolves the host_pool DSN from the per-env ``secrets.toml``; so we
    first reconstruct that file from the env-keyed Vault secrets the deploy
    published. Idempotent against an already-destroyed env.
    """
    env_root_dir(name).mkdir(parents=True, exist_ok=True)
    _reconstruct_env_secrets_file(name)
    sub_env = build_minds_env_subprocess_env(name)
    logger.info("Destroying env {!r}", name)
    completed = subprocess.run(
        ["uv", "run", "minds-admin", "env", "destroy"],
        env=sub_env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_MINDS_DESTROY_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise MindError(
            f"`minds-admin env destroy` for {name!r} exited {completed.returncode}.\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    for role in _DEFAULT_SHARED_ENV_ROLES:
        try:
            delete_shared_env_secrets(env_name=name, role=role)
        except (MindError, httpx.HTTPError) as exc:
            logger.warning("Failed to delete per-env Vault secrets for env {!r} role {!r}: {}", name, role, exc)
    try:
        delete_pool_secrets(env_name=name)
    except (MindError, httpx.HTTPError) as exc:
        logger.warning("Failed to delete per-env pool Vault secrets for env {!r}: {}", name, exc)
    _mark_status(NonEmptyStr(str(name)), kind=LedgerKind.ENV, run_id=run_id, status=LedgerStatus.DESTROYED)


def _reconstruct_env_secrets_file(name: DevEnvName) -> None:
    """Rebuild ``~/.minds-<name>/secrets.toml`` from the env-keyed Vault secrets.

    A no-op when the local file already exists (same-machine destroy) or when no
    per-env secrets are in Vault (already cleaned up / not a ci env we deployed).
    """
    if secrets_file(name).is_file():
        return
    for role in _DEFAULT_SHARED_ENV_ROLES:
        try:
            secrets = read_shared_env_secrets(env_name=name, role=role)
        except (MindError, httpx.HTTPError) as exc:
            logger.warning(
                "Could not reconstruct secrets.toml for env {!r} from Vault (role {!r}): {}", name, role, exc
            )
            continue
        if secrets:
            write_secrets_file({key: SecretStr(value) for key, value in secrets.items()}, name=name)
            return


# ---------------------------------------------------------------------------
# Name + age sweep
# ---------------------------------------------------------------------------


_CI_ENV_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^ci-(\d{8}t\d{6}z)")
# The bucket sweep reads both of its inputs from the ci tier's own Vault
# prefix, which is all the cleanup job's Vault role (minds_ci_env_gh) is
# scoped to. That works because the ci and dev tiers share one Cloudflare
# account AND one SuperTokens core (the ci tier's SUPERTOKENS_CONNECTION_URI
# is the same core the dev-* apps live on) -- so the ci secrets can see every
# account that is able to create a bucket in that Cloudflare account, which is
# exactly what the "no live owner" rule needs. See imbue.minds_admin.envs.r2_cleanup.
_CI_VAULT_PREFIX: Final[str] = "secrets/minds/ci"


def _parse_ci_env_timestamp(stamp: str) -> datetime:
    """Parse the ``YYYYMMDDtHHMMSSz`` timestamp embedded in a ``ci-*`` env name."""
    return datetime.strptime(stamp, "%Y%m%dt%H%M%Sz").replace(tzinfo=timezone.utc)


def _list_stale_ci_env_names(*, cutoff: datetime) -> list[DevEnvName]:
    """Enumerate Modal environments named ``ci-<timestamp>...`` older than ``cutoff``.

    Lists Modal envs (the cross-runner source of truth -- a leaked env from a
    prior CI run is not on this runner's local disk) and filters to ``ci-*``
    names whose embedded timestamp predates the cutoff.
    """
    sub_env = dict(os.environ)
    profile = modal_profile_for_tier_or_none(tier_for_env_name(_CI_TIER_PROBE_ENV_NAME))
    if profile is not None:
        sub_env[MODAL_PROFILE_ENV_VAR] = profile
    result = subprocess.run(
        ["uv", "run", "modal", "environment", "list", "--json"],
        env=sub_env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_MODAL_ENV_LIST_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise MindError(f"`modal environment list --json` exited {result.returncode}: {result.stderr.strip()!r}")
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MindError(f"`modal environment list --json` returned non-JSON: {result.stdout[:200]!r}") from exc
    stale: list[DevEnvName] = []
    for entry in entries:
        raw_name = entry.get("Name") or entry.get("name")
        if not raw_name:
            continue
        match = _CI_ENV_NAME_PATTERN.match(raw_name)
        if match is None:
            continue
        if _parse_ci_env_timestamp(match.group(1)) < cutoff:
            stale.append(DevEnvName(raw_name))
    return stale


def _sweep_stale_envs(max_age_hours: int = _DEFAULT_MAX_RESOURCE_AGE_HOURS) -> None:
    """Enumerate ``ci-*`` Modal envs; destroy anything older than ``max_age_hours``.

    The backstop for CI envs that leaked because a per-run destroy never ran
    (job hard-crash / cancellation). Destroys by name (re-deriving cloud
    resources from Vault) so it works even though the leaked env has no local
    state on this runner.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    stale = _list_stale_ci_env_names(cutoff=cutoff)
    if not stale:
        logger.info("Name+age sweep: no ci-* envs older than {} ({}h).", cutoff.isoformat(), max_age_hours)
        return
    logger.info("Name+age sweep: destroying {} stale ci-* env(s) older than {}h.", len(stale), max_age_hours)
    for name in stale:
        match = _CI_ENV_NAME_PATTERN.match(str(name))
        # The per-env Vault secrets are keyed by env name, so _destroy_env can
        # reconstruct secrets.toml + delete them for any leaked env here. The
        # run_id (parsed from the env-name timestamp) is only for the ledger row.
        run_id = RunId(match.group(1)) if match is not None else _mint_run_id()
        try:
            _destroy_env(name, run_id=run_id)
        except (MindError, httpx.HTTPError) as exc:
            logger.error("Sweep failed to destroy {!r}: {}", name, exc)


# ---------------------------------------------------------------------------
# Test-process env + JSON
# ---------------------------------------------------------------------------


def _write_deployment_envs_json(
    *,
    shared_envs: dict[SharedEnvRole, SharedEnvUrls],
    default_workspace_template: DefaultWorkspaceTemplateRef,
    run_id: RunId,
    target_path: Path = _DEPLOYMENT_ENVS_JSON_PATH,
) -> Path:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    config = DeploymentEnvsConfig(
        shared_envs=shared_envs, default_workspace_template=default_workspace_template, run_id=run_id
    )
    target_path.write_text(config.model_dump_json(indent=2))
    return target_path


def _build_pytest_env(
    *,
    deployment_envs_json_path: Path,
    mailtm_address: str | None,
    mailtm_jwt: SecretStr | None,
    shared_env_secrets: dict[SharedEnvRole, dict[str, SecretStr]],
) -> dict[str, str]:
    """Build the env dict the pytest subprocess inherits.

    Reads from the current process env first so ``VAULT_TOKEN`` /
    ``VAULT_ADDR`` / ``VAULT_NAMESPACE`` / ``ANTHROPIC_API_KEY`` pass
    through unmodified.
    """
    env = dict(os.environ)
    env[DEPLOYMENT_ENVS_JSON_ENV_VAR] = str(deployment_envs_json_path)
    # The deployment/services tests each deploy real cloud envs and run for
    # minutes; raise the pytest global-duration deadline well above the default
    # so the suite isn't failed for simply being slow (an operator override of
    # the env var still wins). The per-test `@pytest.mark.timeout` decorators
    # remain the real per-test guards.
    env.setdefault("PYTEST_MAX_DURATION_SECONDS", str(_PYTEST_MAX_DURATION_SECONDS))
    if mailtm_address and mailtm_jwt:
        env[MAILTM_ADDRESS_ENV_VAR] = mailtm_address
        env[MAILTM_JWT_ENV_VAR] = mailtm_jwt.get_secret_value()
    for role, secrets in shared_env_secrets.items():
        prefix = f"{SHARED_ENV_SECRET_ENV_VAR_PREFIX}{str(role).upper()}_"
        for key, value in secrets.items():
            env[f"{prefix}{key}"] = value.get_secret_value()
    return env


def _invoke_pytest_for_mark(
    mark: str,
    *,
    env: dict[str, str],
    extra_args: tuple[str, ...] = (),
) -> int:
    """Run ``uv run pytest -m <mark> <targets>``; return exit code.

    ``extra_args`` lets ``services-against`` override the default test
    target (the whole deployment_tests/ dir) with whichever specific
    test files / nodeids the operator passed on the command line. The
    default ``run`` flow leaves it empty so the full directory is collected.
    """
    targets = list(extra_args) if extra_args else [str(_REPO_ROOT / "apps" / "minds" / "deployment_tests")]
    cmd = [
        "uv",
        "run",
        "pytest",
        "-m",
        mark,
        "--no-cov",
        "-p",
        "no:xdist",
        *targets,
    ]
    logger.info("Running: {}", " ".join(cmd))
    completed = subprocess.run(cmd, env=env, cwd=str(_REPO_ROOT), check=False)
    return completed.returncode


# ---------------------------------------------------------------------------
# Top-level click CLI
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Orchestrate the minds_deployment + minds_services pytest suites."""


@cli.command()
@click.option(
    "--keep-on-failure", is_flag=True, default=False, help="Leave ephemeral envs from failing tests in place."
)
def run(keep_on_failure: bool) -> None:
    """Full flow: sweep, DEFAULT_WORKSPACE_TEMPLATE push, mail.tm, shared envs, pytest x2, teardown."""
    run_id = _mint_run_id()
    logger.info("Starting orchestrator run {}", run_id)

    _validate_default_workspace_template_worktree()
    _sweep_stale_envs()

    default_workspace_template_branch = _push_default_workspace_template_test_branch(run_id=run_id)
    mailtm = _create_mailtm_account(run_id=run_id)

    shared_env_urls: dict[SharedEnvRole, SharedEnvUrls] = {}
    shared_env_secrets: dict[SharedEnvRole, dict[str, SecretStr]] = {}
    deploy_failure: MindError | None = None
    try:
        for role in _DEFAULT_SHARED_ENV_ROLES:
            env_name = _mint_shared_env_name(run_id=run_id, role=role)
            _append_ledger_entry(
                LedgerEntry(
                    kind=LedgerKind.ENV,
                    name=NonEmptyStr(str(env_name)),
                    created_at=datetime.now(timezone.utc),
                    run_id=run_id,
                    status=LedgerStatus.ACTIVE,
                )
            )
            shared_env_urls[role] = _deploy_shared_env(name=env_name, role=role)
    except MindError as exc:
        deploy_failure = exc
        logger.error("Shared env deploy failed: {}", exc)

    pytest_envs_path = _write_deployment_envs_json(
        shared_envs=shared_env_urls,
        default_workspace_template=DefaultWorkspaceTemplateRef(
            worktree_path=_DEFAULT_WORKSPACE_TEMPLATE_WORKTREE_PATH,
            test_branch=NonEmptyStr(default_workspace_template_branch),
            test_remote=NonEmptyStr(_DEFAULT_WORKSPACE_TEMPLATE_REMOTE_URL),
        ),
        run_id=run_id,
    )
    pytest_env = _build_pytest_env(
        deployment_envs_json_path=pytest_envs_path,
        mailtm_address=str(mailtm.address),
        mailtm_jwt=mailtm.jwt,
        shared_env_secrets=shared_env_secrets,
    )

    # minds_deployment tests use only the ephemeral_env fixture (they mint
    # their own ci-* env per test) and do not depend on the shared envs,
    # so they run regardless of whether the shared-env stand-up succeeded.
    # minds_services tests depend on shared_env(role=...) URLs+secrets, so
    # they are skipped when shared-env deploy failed.
    deployment_rc = _invoke_pytest_for_mark("minds_deployment", env=pytest_env)
    services_rc = _invoke_pytest_for_mark("minds_services", env=pytest_env) if deploy_failure is None else 1

    teardown_failures = _teardown_run(
        run_id=run_id,
        mailtm_account=mailtm,
        default_workspace_template_branch=default_workspace_template_branch,
        keep_on_failure=keep_on_failure,
        tests_failed=(deployment_rc != 0 or services_rc != 0),
    )
    _drop_destroyed_rows_if_drained()

    exit_code = 0
    if deploy_failure is not None or deployment_rc != 0 or services_rc != 0 or teardown_failures:
        exit_code = 1
    logger.info("Orchestrator run {} done -- exit code {}", run_id, exit_code)
    sys.exit(exit_code)


@cli.command()
@click.option(
    "--max-age-hours",
    type=int,
    default=_DEFAULT_MAX_RESOURCE_AGE_HOURS,
    help="Destroy ci-* envs whose embedded timestamp is older than this many hours.",
)
def sweep(max_age_hours: int) -> None:
    """Enumerate ci-* Modal envs and destroy any older than the age threshold.

    The cross-run leaked-resource backstop (a per-run destroy that never
    fired because its job hard-crashed / was cancelled). Run on its own CI
    runner, so it relies on the Modal-side enumeration rather than local
    state.

    Then sweep the R2 buckets imbue-cloud backups provisioned for accounts
    that no longer exist -- env destroy never deleted them, so every CI run
    that exercised backups leaked one (see :mod:`imbue.minds_admin.envs.r2_cleanup`
    for why "no live owner" is the only safe rule on an account the dev tier
    shares).
    """
    _sweep_stale_envs(max_age_hours=max_age_hours)
    _sweep_orphaned_buckets()


def _sweep_orphaned_buckets(*, is_dry_run: bool = False) -> tuple[str, ...]:
    """Delete R2 buckets whose owning account is gone; never fails the caller."""
    try:
        cloudflare_secrets = read_vault_kv(VaultPath(f"{_CI_VAULT_PREFIX}/cloudflare"))
        supertokens_secrets = read_vault_kv(VaultPath(f"{_CI_VAULT_PREFIX}/supertokens"))
    except MindError as exc:
        logger.error("R2 sweep skipped: could not read the Cloudflare / SuperTokens secrets: {}", exc)
        return ()
    try:
        return sweep_orphaned_r2_buckets(
            CloudflareR2Credentials(
                account_id=cloudflare_secrets["CLOUDFLARE_ACCOUNT_ID"],
                api_token=SecretStr(cloudflare_secrets["CLOUDFLARE_API_TOKEN"]),
            ),
            SuperTokensCoreCredentials(
                connection_uri=supertokens_secrets["SUPERTOKENS_CONNECTION_URI"],
                api_key=SecretStr(supertokens_secrets["SUPERTOKENS_API_KEY"]),
            ),
            is_dry_run=is_dry_run,
        )
    except (R2CleanupError, KeyError) as exc:
        # The bucket sweep is a backstop; a failure here must not fail the
        # cleanup job (whose primary duty is destroying leaked envs).
        logger.error("R2 sweep failed: {}", exc)
        return ()


@cli.command(name="sweep-buckets")
@click.option("--dry-run", is_flag=True, help="List the ownerless buckets without deleting anything.")
def sweep_buckets(dry_run: bool) -> None:
    """Sweep R2 buckets whose owning account no longer exists (backups leak them)."""
    swept = _sweep_orphaned_buckets(is_dry_run=dry_run)
    verb = "Would delete" if dry_run else "Deleted"
    write_stdout_line(f"{verb} {len(swept)} ownerless R2 bucket(s).")


@cli.command()
def cleanup() -> None:
    """Walk every ledger entry across all prior runs; tear each down; drop the file when drained."""
    entries = _read_ledger_entries()
    if not entries:
        write_stdout_line("Ledger is empty -- nothing to clean up.")
        return
    latest = _latest_status_by_name(entries)
    leftovers = [entry for entry in latest.values() if entry.status != LedgerStatus.DESTROYED]
    if not leftovers:
        _drop_destroyed_rows_if_drained()
        write_stdout_line("Ledger had only destroyed entries -- file removed.")
        return
    write_stdout_line(f"Cleaning up {len(leftovers)} active+leaked entries from prior runs...")
    failures = 0
    for entry in leftovers:
        try:
            match entry.kind:
                case LedgerKind.ENV:
                    _destroy_env(DevEnvName(str(entry.name)), run_id=entry.run_id)
                case LedgerKind.DEFAULT_WORKSPACE_TEMPLATE_BRANCH:
                    _delete_default_workspace_template_test_branch(str(entry.name), run_id=entry.run_id)
                case LedgerKind.MAILTM_ACCOUNT:
                    logger.warning(
                        "mail.tm account {} cleanup needs the JWT, which we did not persist; "
                        "the account will expire naturally.",
                        entry.name,
                    )
                    _mark_status(
                        entry.name,
                        kind=LedgerKind.MAILTM_ACCOUNT,
                        run_id=entry.run_id,
                        status=LedgerStatus.DESTROYED,
                    )
                case _ as unreachable:
                    assert_never(unreachable)
        except (MindError, httpx.HTTPError) as exc:
            logger.error("Cleanup failed for {} {}: {}", entry.kind, entry.name, exc)
            failures += 1
    _drop_destroyed_rows_if_drained()
    if failures:
        sys.exit(1)


@cli.command(name="deployment-only")
@click.argument("tests", nargs=-1)
def deployment_only(tests: tuple[str, ...]) -> None:
    """Run only the ``minds_deployment`` pytest batch (no shared env stand-up).

    For iterating on the ``minds_deployment`` tests (those that mint
    their own ephemeral env via the ``ephemeral_env`` fixture) without
    paying for the shared-env-deploy + mail.tm-account setup that the
    main ``run`` command does. The DEFAULT_WORKSPACE_TEMPLATE worktree is still validated up
    front so tests that create real minds agents have a template ref to
    point at; pass test files / nodeids positionally.

    Operator must have ``vault login``-ed (the in-test ``minds-admin env
    deploy`` subprocess reads tier secrets from Vault).
    """
    _validate_default_workspace_template_worktree()
    run_id = _mint_run_id()

    pytest_envs_path = _write_deployment_envs_json(
        shared_envs={},
        default_workspace_template=DefaultWorkspaceTemplateRef(
            worktree_path=_DEFAULT_WORKSPACE_TEMPLATE_WORKTREE_PATH
        ),
        run_id=run_id,
    )
    pytest_env = _build_pytest_env(
        deployment_envs_json_path=pytest_envs_path,
        mailtm_address=None,
        mailtm_jwt=None,
        shared_env_secrets={},
    )

    test_targets = tuple(tests) if tests else ()
    rc = _invoke_pytest_for_mark("minds_deployment", env=pytest_env, extra_args=test_targets)
    _drop_destroyed_rows_if_drained()
    sys.exit(rc)


@cli.command()
@click.argument("role", default="default")
def up(role: str) -> None:
    """Local iterate: stand up a shared env + print a ready-to-paste pytest command."""
    run_id = _mint_run_id()
    role_key = SharedEnvRole(role)
    _validate_default_workspace_template_worktree()
    env_name = _mint_shared_env_name(run_id=run_id, role=role_key)
    _append_ledger_entry(
        LedgerEntry(
            kind=LedgerKind.ENV,
            name=NonEmptyStr(str(env_name)),
            created_at=datetime.now(timezone.utc),
            run_id=run_id,
            status=LedgerStatus.ACTIVE,
        )
    )
    urls = _deploy_shared_env(name=env_name, role=role_key)
    state_path = _ITERATE_STATE_DIR / f"iterate-{role}.json"
    _write_deployment_envs_json(
        shared_envs={role_key: urls},
        default_workspace_template=DefaultWorkspaceTemplateRef(
            worktree_path=_DEFAULT_WORKSPACE_TEMPLATE_WORKTREE_PATH
        ),
        run_id=run_id,
        target_path=state_path,
    )
    write_stdout_line(f"Shared env {env_name!r} (role={role!r}) is up.")
    write_stdout_line(f"State file: {state_path}")
    write_stdout_line("Run the tests with:")
    write_stdout_line(
        f"  {DEPLOYMENT_ENVS_JSON_ENV_VAR}={state_path} uv run pytest -m minds_services apps/minds/deployment_tests/"
    )
    write_stdout_line("Tear down with: just minds-test-deployment-down")


@cli.command(name="bake-pool")
@click.option(
    "--state-file",
    default=str(_ITERATE_STATE_DIR / "iterate-default.json"),
    show_default=True,
    help="The deployment_envs.json written by `up` for the env to provision the pool for.",
)
@click.option(
    "--count",
    type=int,
    default=_DEFAULT_CI_POOL_SLICE_COUNT,
    show_default=True,
    help="Slices to bake (remote-workspace test roster + spares).",
)
@click.option(
    "--template-ref",
    default=None,
    help=(
        "default-workspace-template ref to bake (branch, tag, or full SHA); frozen to a SHA at bake "
        "time. Defaults to main. Mutually exclusive with --template-dir (a local checkout bakes as-is "
        "at its current HEAD)."
    ),
)
@click.option(
    "--template-dir",
    default=None,
    type=click.Path(exists=True),
    help="Bake from this local template checkout instead of cloning --template-ref.",
)
@click.option(
    "--region",
    default=_DEFAULT_CI_POOL_REGION,
    show_default=True,
    help="Lease-region label stamped on the rows; also selects which CI box (by datacenter) to bake on.",
)
def bake_pool(state_file: str, count: int, template_ref: str | None, template_dir: str | None, region: str) -> None:
    """Pre-provision leasable pool slices for an up'd ci env (the CI release flow's bake stage).

    Sweeps stale CI slices off the standing boxes, imports the ready box rows from
    the CI infra DB into the env's own host_pool DB, bakes ``count`` slices from the
    requested template content (content-addressed image cache, so identical content
    re-bakes warm), and records the stamped ``(repo_url, repo_branch_or_tag)``
    identity into the state file for the tests to lease fast-path against. See
    specs/remote-workspaces-in-ci.md.
    """
    if template_ref is not None and template_dir is not None:
        raise click.UsageError(
            "--template-ref and --template-dir are mutually exclusive: a local --template-dir checkout "
            "bakes as-is at its current HEAD, so an explicit ref would be silently ignored"
        )
    state_path = Path(state_file)
    if not state_path.is_file():
        raise click.ClickException(f"No state file at {state_path}; run `up` first.")
    config = DeploymentEnvsConfig.model_validate_json(state_path.read_text())
    default_role = SharedEnvRole("default")
    if default_role not in config.shared_envs:
        raise click.ClickException(f"State file {state_path} has no 'default' shared env to bake for.")
    env_name = config.shared_envs[default_role].env_name

    infra_dsn = _read_ci_infra_pool_dsn()
    target_dsn = _read_env_host_pool_dsn(env_name)
    sub_env = build_minds_env_subprocess_env(env_name)

    # 1. Sweep stale CI slices first, so a wedged prior run cannot eat the slots
    #    this bake needs. Sweep failures are non-fatal here (the bake's own
    #    capacity check reports precisely if slots are genuinely short).
    sweep_rc = _run_minds_admin_streaming(
        ["server", "sweep-ci-slices", "--database-url", infra_dsn],
        sub_env=sub_env,
        timeout_seconds=_MINDS_SWEEP_TIMEOUT_SECONDS,
    )
    if sweep_rc != 0:
        logger.warning("CI slice sweep exited {} (continuing; the bake's capacity check is authoritative)", sweep_rc)

    # 2. Make the standing boxes leasable from this env.
    import_rc = _run_minds_admin_streaming(
        ["server", "import-boxes", "--source-database-url", infra_dsn, "--database-url", target_dsn],
        sub_env=sub_env,
        timeout_seconds=_MINDS_SWEEP_TIMEOUT_SECONDS,
    )
    if import_rc != 0:
        raise click.ClickException(f"`minds-admin server import-boxes` exited {import_rc}")
    server_id = _select_ci_box_for_region(target_dsn, region=region)

    # 3. Hand the template read key to the (separately-authorized) test runner
    #    via the env's per-run Vault path: the CI test job's Vault role cannot
    #    read the static minds/ci/dwt entry, only minds/ci/runs/* (see
    #    publish_pool_secrets). Published BEFORE the bake: the CI job's Vault
    #    token TTL (30 min) would expire across a cold bake. Only the CI clone
    #    path needs this -- a local --template-dir iterate run already has the
    #    checkout, and the operator's token may lack runs/* write. An orphan
    #    entry from a failed bake is deleted by env destroy (and the test job
    #    only reads it when the state file carries pool info).
    if template_dir is None:
        dwt_key_b64 = os.environ.get(_DWT_READ_KEY_ENV_VAR) or _read_dwt_key_from_vault_or_none()
    else:
        dwt_key_b64 = None
    if dwt_key_b64:
        publish_pool_secrets(env_name=env_name, secrets={POOL_DWT_READ_KEY_SECRET_KEY: dwt_key_b64})

    # 4. Bake. The stamped repo_branch_or_tag is the resolved template SHA, so a
    #    fast-path lease from this run can only adopt this run's own bake.
    with _resolved_template_checkout(
        template_ref=template_ref or "main", template_dir=template_dir, key_b64=dwt_key_b64
    ) as (
        workspace_dir,
        resolved_sha,
    ):
        # The same canonical repo identity `pool create` stamps on the rows
        # (derived from the checkout's origin); recorded so fast-path tests
        # request exactly the (repo_url, repo_branch_or_tag) pair that matches.
        stamped_repo_url = canonicalize_repo_source(str(workspace_dir))
        bake_rc = _run_minds_admin_streaming(
            [
                "pool",
                "create",
                "--count",
                str(count),
                "--region",
                region,
                "--workspace-dir",
                str(workspace_dir),
                "--repo-branch-or-tag",
                resolved_sha,
                "--mngr-source",
                str(_REPO_ROOT),
                "--content-addressed-cache",
                "--server-id",
                server_id,
                "--database-url",
                target_dsn,
                # Bake the whole roster in one wave: with the tar warmed (or seeded
                # once), each slice is carve + docker load + finalize, and a second
                # wave would serialize minutes for no contention benefit.
                "--max-concurrency",
                str(min(count, _MAX_CI_BAKE_CONCURRENCY)),
            ],
            sub_env=sub_env,
            timeout_seconds=_MINDS_BAKE_TIMEOUT_SECONDS,
        )
    if bake_rc != 0:
        raise click.ClickException(f"`minds-admin pool create` exited {bake_rc}")

    updated_config = config.model_copy_update(
        to_update(
            config.field_ref().pool,
            PoolProvisionInfo(
                repo_url=NonEmptyStr(stamped_repo_url),
                repo_branch_or_tag=NonEmptyStr(resolved_sha),
                region=NonEmptyStr(region),
                slice_count=count,
            ),
        ),
    )
    state_path.write_text(updated_config.model_dump_json(indent=2))
    write_stdout_line(f"Baked {count} slice(s) for env {env_name!r} at template {resolved_sha} (region {region}).")


@cli.command(name="warm-pool-cache")
@click.option(
    "--template-ref",
    default=None,
    help=(
        "default-workspace-template ref whose content to pre-warm (branch, tag, or full SHA); frozen to "
        "a SHA at clone time. Defaults to main. Mutually exclusive with --template-dir."
    ),
)
@click.option(
    "--template-dir",
    default=None,
    type=click.Path(exists=True),
    help="Warm from this local template checkout instead of cloning --template-ref.",
)
@click.option(
    "--region",
    default=_DEFAULT_CI_POOL_REGION,
    show_default=True,
    help="Lease region whose CI box to warm (must match the bake stage's --region so both pick the same box).",
)
def warm_pool_cache(template_ref: str | None, template_dir: str | None, region: str) -> None:
    """Pre-warm the CI box's image cache for the run's template content (the CI warm job's entrypoint).

    Selects the same box the bake stage will select (shared deterministic rule over the
    infra DB's rows, which import-boxes copies id-preserving into the per-run env), then
    runs ``minds-admin pool warm-cache`` against it so the run's cold seed build overlaps
    the env deploy instead of following it. Needs no minds env: the box row is read from
    the CI infra DB and the pool key is handed to the subprocess from the ci tier's Vault
    entry. Exit status mirrors the verb's, and the CI job treats it as advisory (the bake
    stage's own seed phase is the fallback). See specs/remote-workspaces-in-ci.md.
    """
    if template_ref is not None and template_dir is not None:
        raise click.UsageError(
            "--template-ref and --template-dir are mutually exclusive: a local --template-dir checkout "
            "warms as-is at its current content, so an explicit ref would be silently ignored"
        )
    infra_dsn = _read_ci_infra_pool_dsn()
    server_id = _select_ci_box_for_region(infra_dsn, region=region)
    pool_key = read_vault_kv(VaultPath(f"{_CI_VAULT_PREFIX}/pool-ssh")).get("POOL_SSH_PRIVATE_KEY", "")
    if not pool_key:
        raise MindError(
            f"Vault entry {_CI_VAULT_PREFIX}/pool-ssh is missing POOL_SSH_PRIVATE_KEY; cannot SSH the CI box."
        )
    sub_env = dict(os.environ)
    sub_env["POOL_SSH_PRIVATE_KEY"] = pool_key
    if template_dir is None:
        dwt_key_b64 = os.environ.get(_DWT_READ_KEY_ENV_VAR) or _read_dwt_key_from_vault_or_none()
    else:
        dwt_key_b64 = None
    with _resolved_template_checkout(
        template_ref=template_ref or "main", template_dir=template_dir, key_b64=dwt_key_b64
    ) as (
        workspace_dir,
        resolved_sha,
    ):
        logger.info("Warming the CI box {} image cache for template {}", server_id, resolved_sha)
        warm_rc = _run_minds_admin_streaming(
            [
                "pool",
                "warm-cache",
                "--server-id",
                server_id,
                "--workspace-dir",
                str(workspace_dir),
                "--mngr-source",
                str(_REPO_ROOT),
                "--content-addressed-cache",
                "--database-url",
                infra_dsn,
            ],
            sub_env=sub_env,
            timeout_seconds=_MINDS_WARM_TIMEOUT_SECONDS,
        )
    sys.exit(warm_rc)


def _read_ci_infra_pool_dsn() -> str:
    """The CI infra DB's pooled DSN (the canonical registry of the standing CI boxes)."""
    secrets = read_vault_kv(VaultPath(f"{_CI_VAULT_PREFIX}/neon"))
    dsn = secrets.get("DATABASE_URL", "")
    if not dsn:
        raise MindError(
            f"Vault entry {_CI_VAULT_PREFIX}/neon is missing DATABASE_URL; the CI infra pool DB is not "
            "set up -- see specs/remote-workspaces-in-ci.md (phase 0)."
        )
    return dsn


def _read_env_host_pool_dsn(env_name: DevEnvName) -> str:
    """The up'd env's own host_pool DSN, from the secrets.toml its deploy wrote."""
    _reconstruct_env_secrets_file(env_name)
    secrets_model = read_secrets_file(env_name)
    dsn_secret = secrets_model.secrets.get("NEON_HOST_POOL_DSN")
    if dsn_secret is None or not dsn_secret.get_secret_value():
        raise MindError(f"Env {env_name!r} secrets.toml has no NEON_HOST_POOL_DSN; was its deploy successful?")
    return dsn_secret.get_secret_value()


def _run_minds_admin_streaming(args: list[str], *, sub_env: dict[str, str], timeout_seconds: int) -> int:
    """Run a ``minds-admin`` subcommand streaming to our stdout/stderr; return its exit code.

    The logged command line redacts DSN values: several of these subcommands take
    ``--database-url`` arguments whose values are credentials, and this log line
    lands in CI logs.
    """
    cmd = ["uv", "run", "minds-admin", *args]
    redacted = [re.sub(r"postgres(?:ql)?://\S+", r"postgresql://<redacted>", part) for part in cmd]
    logger.info("Running: {}", " ".join(redacted))
    completed = subprocess.run(cmd, env=sub_env, cwd=str(_REPO_ROOT), timeout=timeout_seconds, check=False)
    return completed.returncode


def _select_ci_box_for_region(target_dsn: str, *, region: str) -> str:
    """Pick the ready box (by id) in the requested lease region's datacenter.

    The selection itself is the shared deterministic rule
    (:func:`find_first_ready_server_in_datacenter`), so the warm job (selecting
    against the infra DB) and the bake stage (selecting against the per-run env's
    DB, whose rows import-boxes copied id-preserving from the infra DB) always
    pick the same box.
    """
    datacenter = OVH_DATACENTER_CODE_BY_US_REGION.get(region)
    if datacenter is None:
        raise click.ClickException(
            f"--region {region!r} has no known datacenter mapping; pass one of "
            f"{sorted(OVH_DATACENTER_CODE_BY_US_REGION)}"
        )
    conn = psycopg2.connect(target_dsn)
    try:
        servers = fetch_servers(conn)
    finally:
        conn.close()
    chosen = find_first_ready_server_in_datacenter(servers, datacenter)
    if chosen is None:
        raise click.ClickException(
            f"No ready CI box in datacenter {datacenter!r} ({region}) in this pool DB; "
            "check the CI infra DB's bare_metal_servers rows (and, for the bake stage, that "
            "import-boxes ran)."
        )
    return str(chosen.id)


@contextmanager
def _resolved_template_checkout(
    *, template_ref: str, template_dir: str | None, key_b64: str | None
) -> Iterator[tuple[Path, str]]:
    """Yield ``(workspace_dir, resolved_sha)`` for the template content to bake.

    A local ``template_dir`` is used as-is (its HEAD SHA is the stamp; uncommitted
    changes bake but the stamp stays the SHA, matching dev-bake looseness). Without
    one, the template repo is cloned to the standard
    ``.external_worktrees/default-workspace-template`` location -- kept in place so
    the fast-path create tests can run ``mngr create`` from it later -- with the
    ref checked out detached, authenticated by the ``key_b64`` read-only deploy
    key (the caller resolves it from ``MINDS_CI_DWT_KEY_B64`` or Vault), falling
    back to ambient git credentials when it is None. An operator who already has
    a checkout there must pass it explicitly via ``--template-dir`` (this never
    mutates an existing checkout).
    """
    if template_dir is not None:
        checkout = Path(template_dir).resolve()
        sha = _git_head_sha(checkout)
        yield checkout, sha
        return
    checkout = _DEFAULT_WORKSPACE_TEMPLATE_WORKTREE_PATH
    if checkout.exists():
        raise click.ClickException(
            f"{checkout} already exists; pass it explicitly with --template-dir {checkout} "
            "(bake-pool never checks out a different ref under an existing checkout)."
        )
    clone_env = dict(os.environ)
    clone_url = _DEFAULT_WORKSPACE_TEMPLATE_HTTPS_URL
    key_dir: Path | None = None
    try:
        if key_b64:
            key_dir = Path(tempfile.mkdtemp(prefix="minds-ci-dwt-key-"))
            key_path = key_dir / "id"
            key_path.write_bytes(base64.b64decode(key_b64))
            key_path.chmod(0o600)
            clone_env["GIT_SSH_COMMAND"] = (
                f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
            )
            clone_url = _DEFAULT_WORKSPACE_TEMPLATE_REMOTE_URL
        checkout.parent.mkdir(parents=True, exist_ok=True)
        _run_git_checked(["clone", "--quiet", clone_url, str(checkout)], env=clone_env)
        # Try the ref as-is (tag / SHA / local branch), then as a remote branch.
        if _run_git(["-C", str(checkout), "checkout", "--quiet", "--detach", template_ref], env=clone_env) != 0:
            _run_git_checked(
                ["-C", str(checkout), "checkout", "--quiet", "--detach", f"origin/{template_ref}"], env=clone_env
            )
        sha = _git_head_sha(checkout)
        logger.info("Resolved template ref {!r} to {} at {}", template_ref, sha, checkout)
        yield checkout, sha
    finally:
        if key_dir is not None:
            shutil.rmtree(key_dir, ignore_errors=True)


def _read_dwt_key_from_vault_or_none() -> str | None:
    """The read-only template-repo deploy key from Vault, or None (fall back to ambient git creds)."""
    try:
        return read_vault_kv(VaultPath(f"{_CI_VAULT_PREFIX}/dwt")).get("DWT_READ_KEY_B64") or None
    except MindError as exc:
        logger.info("No template deploy key in Vault ({}); using ambient git credentials", exc)
        return None


def _git_head_sha(checkout: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise MindError(f"`git rev-parse HEAD` in {checkout} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def _run_git(args: list[str], *, env: dict[str, str]) -> int:
    completed = subprocess.run(["git", *args], env=env, timeout=_GIT_TIMEOUT_SECONDS, check=False)
    return completed.returncode


def _run_git_checked(args: list[str], *, env: dict[str, str]) -> None:
    return_code = _run_git(args, env=env)
    if return_code != 0:
        raise MindError(f"`git {' '.join(args)}` exited {return_code}")


@cli.command()
@click.argument("role", default="default")
def down(role: str) -> None:
    """Local iterate: tear down whatever ``up`` last stood up for ``role``."""
    state_path = _ITERATE_STATE_DIR / f"iterate-{role}.json"
    if not state_path.is_file():
        write_stdout_line(f"No iterate state file at {state_path}; nothing to tear down.")
        return
    config = DeploymentEnvsConfig.model_validate_json(state_path.read_text())
    for urls in config.shared_envs.values():
        _destroy_env(urls.env_name, run_id=config.run_id)
    state_path.unlink()
    _drop_destroyed_rows_if_drained()


@cli.command(name="services-against")
@click.argument("env_name")
@click.argument("tests", nargs=-1)
@click.option(
    "--no-default-workspace-template-push",
    is_flag=True,
    default=False,
    help="Skip the DEFAULT_WORKSPACE_TEMPLATE branch push (purely backend tests).",
)
def services_against(env_name: str, tests: tuple[str, ...], no_default_workspace_template_push: bool) -> None:
    """Point minds_services tests at an already-deployed dev env (e.g. dev-josh).

    Loads ``~/.minds-<env>/client.toml`` for the URLs + ``~/.minds-<env>/secrets.toml``
    for the per-env SuperTokens + Neon secrets, builds a one-role
    ``deployment_envs.json`` against the ``default`` role, exports the
    per-shared-env secret env vars + the mail.tm credentials (created
    fresh for this run), and shells out to ``uv run pytest -m minds_services``
    with whichever test paths the operator passed.

    Does not touch the target env's cloud state -- no create, no
    destroy, no recover. The DEFAULT_WORKSPACE_TEMPLATE worktree push runs by default so
    tests that create real minds agents can reach the prepared
    template ref; ``--no-default-workspace-template-push`` opts out for purely backend tests.
    """
    dev_env_name = DevEnvName(env_name)
    _validate_default_workspace_template_worktree()
    run_id = _mint_run_id()
    _push_default_workspace_template_test_branch(run_id=run_id) if not no_default_workspace_template_push else None

    target_env_root = Path.home() / f".minds-{dev_env_name}"
    target_client_toml = target_env_root / "client.toml"
    target_secrets_toml = target_env_root / "secrets.toml"
    if not target_client_toml.is_file():
        raise click.ClickException(
            f"No client.toml found at {target_client_toml} for env {env_name!r}. "
            f'Activate + deploy the env first: `eval "$(uv run minds-admin env activate --create --deploy {env_name})" && uv run minds-admin env deploy`.'
        )
    if not target_secrets_toml.is_file():
        raise click.ClickException(
            f"No secrets.toml found at {target_secrets_toml} for env {env_name!r}. "
            "Per-dev-env secrets are written by `minds-admin env deploy`; re-run a deploy if this file is missing."
        )

    client_config = load_client_config(target_client_toml)
    secrets_model = read_secrets_file(dev_env_name)

    default_role = SharedEnvRole("default")
    shared_env_urls = SharedEnvUrls(
        role=default_role,
        env_name=dev_env_name,
        connector_url=client_config.connector_url,
        litellm_proxy_url=client_config.litellm_proxy_url,
    )
    shared_env_secrets: dict[SharedEnvRole, dict[str, SecretStr]] = {
        default_role: {key: value for key, value in secrets_model.secrets.items()}
    }

    mailtm = _create_mailtm_account(run_id=run_id)

    pytest_envs_path = _write_deployment_envs_json(
        shared_envs={default_role: shared_env_urls},
        default_workspace_template=DefaultWorkspaceTemplateRef(
            worktree_path=_DEFAULT_WORKSPACE_TEMPLATE_WORKTREE_PATH
        ),
        run_id=run_id,
    )
    pytest_env = _build_pytest_env(
        deployment_envs_json_path=pytest_envs_path,
        mailtm_address=str(mailtm.address),
        mailtm_jwt=mailtm.jwt,
        shared_env_secrets=shared_env_secrets,
    )

    test_targets = list(tests) if tests else [str(_REPO_ROOT / "apps" / "minds" / "deployment_tests")]
    pytest_argv: tuple[str, ...] = tuple(test_targets)
    rc = _invoke_pytest_for_mark("minds_services", env=pytest_env, extra_args=pytest_argv)

    # Teardown: only the mail.tm account + (if pushed) the DEFAULT_WORKSPACE_TEMPLATE branch
    # need cleanup -- we never created the target dev env.
    teardown_failures = _teardown_run(
        run_id=run_id,
        mailtm_account=mailtm,
        default_workspace_template_branch=NonEmptyStr(f"ci-{run_id}")
        if not no_default_workspace_template_push
        else NonEmptyStr("noop"),
        keep_on_failure=False,
        tests_failed=(rc != 0),
    )
    _drop_destroyed_rows_if_drained()

    sys.exit(1 if rc != 0 or teardown_failures else 0)


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def _teardown_run(
    *,
    run_id: RunId,
    mailtm_account: _MailtmAccount,
    default_workspace_template_branch: str,
    keep_on_failure: bool,
    tests_failed: bool,
) -> int:
    """Tear down everything the current run created; return count of failures."""
    failures = 0
    entries_for_run = [entry for entry in _read_ledger_entries() if entry.run_id == run_id]
    latest = _latest_status_by_name(entries_for_run)
    for entry in latest.values():
        if entry.status == LedgerStatus.DESTROYED:
            continue
        if keep_on_failure and tests_failed and entry.kind == LedgerKind.ENV:
            _mark_status(entry.name, kind=entry.kind, run_id=run_id, status=LedgerStatus.LEAKED)
            logger.info("Marking {} {} as leaked (--keep-on-failure + tests failed)", entry.kind, entry.name)
            continue
        try:
            match entry.kind:
                case LedgerKind.ENV:
                    _destroy_env(DevEnvName(str(entry.name)), run_id=run_id)
                case LedgerKind.DEFAULT_WORKSPACE_TEMPLATE_BRANCH:
                    _delete_default_workspace_template_test_branch(str(entry.name), run_id=run_id)
                case LedgerKind.MAILTM_ACCOUNT:
                    _delete_mailtm_account(entry.name, mailtm_account.jwt, run_id=run_id)
                case _ as unreachable:
                    assert_never(unreachable)
        except (MindError, httpx.HTTPError) as exc:
            logger.error("Teardown failed for {} {}: {}", entry.kind, entry.name, exc)
            failures += 1
    # ``default_workspace_template_branch`` is recorded in the ledger at push time; the teardown loop
    # finds and deletes it via _delete_default_workspace_template_test_branch. The arg is kept on
    # the signature so future callers (e.g. a partial-teardown helper) can
    # target it explicitly.
    _ = default_workspace_template_branch
    return failures


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
