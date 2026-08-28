"""Resolve everything the observability justfile recipes need from Vault into a temp work dir.

Three modes (argv[1]):

- ``resolve <tier> <work_dir>`` -- full provisioning inputs: pulls the tier's
  ``observability``, ``ovh``, and ``cloudflare`` Vault entries, derives the
  telemetry hostname from the tier deploy.toml's ``cloudflare_domain`` and the
  R2 endpoint from the Cloudflare account id, and writes into ``work_dir``:

  - ``ssh_key`` / ``ssh_key.pub`` -- the instance SSH keypair (0600).
  - ``origin.pem`` / ``origin.key`` -- the Cloudflare origin TLS material (0600).
  - ``observability.env`` -- shell-sourceable exports (OVH_*, CLOUDFLARE_*,
    OBSERVABILITY_* incl. the origin-file paths, INGEST_CREDENTIAL_*).
  - ``params.json`` -- ``{tier, telemetry_hostname}``.

- ``collector-env <tier> <sender> <work_dir>`` -- fleet collector inputs for
  one sender class: writes ``collector.env`` with
  ``OBSERVABILITY_INGEST_URL`` + ``OBSERVABILITY_INGEST_CREDENTIAL``. Exits 3
  (distinct from failure) when the tier has no observability Vault entry or
  the sender's credential is still empty, so recipes can skip gracefully.
  Only the RELAY recipes still use this mode: the ``boxes`` sender path is
  served in-process by ``minds-admin server prep`` / ``setup`` (see
  ``imbue.minds_admin.cli._tier_secrets``), which resolve the same Vault
  entry with the same clean-skip semantics.

- ``store-credentials <tier>`` -- reads ``observability provision-accounts``
  JSON on stdin and writes each newly minted ``INGEST_CREDENTIAL_*`` back to
  the tier's Vault entry (existing credentials are never rewritten).

Run from the repo root via ``uv run python scripts/provision_observability_config.py``
(the workspace venv provides the imbue.minds imports).
"""

import json
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.minds.envs.primitives import VaultSecretNotFoundError
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.envs.vault_reader import write_vault_kv

# Exit code the collector-env mode uses for "observability is not configured
# for this tier (yet)" -- recipes treat it as a graceful skip, everything else
# as a real failure.
NOT_CONFIGURED_EXIT_CODE: Final[int] = 3

_INGEST_CREDENTIAL_KEY_BY_SENDER: Final[dict[str, str]] = {
    "modal": "INGEST_CREDENTIAL_MODAL",
    "boxes": "INGEST_CREDENTIAL_BOXES",
    "relays": "INGEST_CREDENTIAL_RELAYS",
}


def _telemetry_hostname_for_tier(tier: str) -> str:
    deploy_toml = Path("apps/minds/imbue/minds/config/envs") / tier / "deploy.toml"
    with deploy_toml.open("rb") as handle:
        cloudflare_domain = tomllib.load(handle)["cloudflare_domain"]
    return f"telemetry.{cloudflare_domain}"


def _write_private_file(path: Path, content: str) -> None:
    path.write_text(content.rstrip("\n") + "\n")
    path.chmod(0o600)


def _resolve(tier: str, work_dir: Path) -> None:
    vault_prefix = f"secrets/minds/{tier}"
    observability = read_vault_kv(VaultPath(f"{vault_prefix}/observability"))
    ovh = read_vault_kv(VaultPath(f"{vault_prefix}/ovh"))
    cloudflare = read_vault_kv(VaultPath(f"{vault_prefix}/cloudflare"))
    telemetry_hostname = _telemetry_hostname_for_tier(tier)

    # Multi-line material goes to owner-only files; everything else is
    # exported for the observability CLI to read from the environment.
    _write_private_file(work_dir / "ssh_key", observability["OBSERVABILITY_SSH_PRIVATE_KEY"])
    _write_private_file(work_dir / "ssh_key.pub", observability["OBSERVABILITY_SSH_PUBLIC_KEY"])
    _write_private_file(work_dir / "origin.pem", observability["OBSERVABILITY_ORIGIN_TLS_CERT"])
    _write_private_file(work_dir / "origin.key", observability["OBSERVABILITY_ORIGIN_TLS_KEY"])

    r2_endpoint = f"https://{cloudflare['CLOUDFLARE_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    export_by_name = {
        "CLOUDFLARE_API_TOKEN": cloudflare["CLOUDFLARE_API_TOKEN"],
        "CLOUDFLARE_ZONE_ID": cloudflare["CLOUDFLARE_ZONE_ID"],
        "OBSERVABILITY_ROOT_EMAIL": observability["OPENOBSERVE_ROOT_EMAIL"],
        "OBSERVABILITY_ROOT_PASSWORD": observability["OPENOBSERVE_ROOT_PASSWORD"],
        "OBSERVABILITY_META_DSN": observability["OPENOBSERVE_META_DSN"],
        "OBSERVABILITY_R2_ENDPOINT": r2_endpoint,
        "OBSERVABILITY_R2_BUCKET": observability["OPENOBSERVE_R2_BUCKET"],
        "OBSERVABILITY_R2_ACCESS_KEY_ID": observability["OPENOBSERVE_R2_ACCESS_KEY_ID"],
        "OBSERVABILITY_R2_SECRET_ACCESS_KEY": observability["OPENOBSERVE_R2_SECRET_ACCESS_KEY"],
        "OBSERVABILITY_ORIGIN_TLS_CERT_FILE": str(work_dir / "origin.pem"),
        "OBSERVABILITY_ORIGIN_TLS_KEY_FILE": str(work_dir / "origin.key"),
        "INGEST_CREDENTIAL_MODAL": observability.get("INGEST_CREDENTIAL_MODAL", ""),
        "INGEST_CREDENTIAL_BOXES": observability.get("INGEST_CREDENTIAL_BOXES", ""),
        "INGEST_CREDENTIAL_RELAYS": observability.get("INGEST_CREDENTIAL_RELAYS", ""),
    }
    ovh_exports = [f"export {key}={shlex.quote(ovh[key])}" for key in sorted(ovh) if ovh[key]]
    other_exports = [f"export {name}={shlex.quote(value)}" for name, value in export_by_name.items()]
    _write_private_file(work_dir / "observability.env", "\n".join(ovh_exports + other_exports))

    (work_dir / "params.json").write_text(json.dumps({"tier": tier, "telemetry_hostname": telemetry_hostname}))


def _collector_env(tier: str, sender: str, work_dir: Path) -> None:
    credential_key = _INGEST_CREDENTIAL_KEY_BY_SENDER.get(sender)
    if credential_key is None:
        raise SystemExit(f"unknown sender {sender!r}; expected one of {sorted(_INGEST_CREDENTIAL_KEY_BY_SENDER)}")
    try:
        observability = read_vault_kv(VaultPath(f"secrets/minds/{tier}/observability"))
    except VaultSecretNotFoundError:
        logger.info("No observability Vault entry for tier {}; collector install will be skipped", tier)
        sys.exit(NOT_CONFIGURED_EXIT_CODE)
    credential = observability.get(credential_key, "")
    if not credential:
        logger.info("Tier {} has no {} yet; collector install will be skipped", tier, credential_key)
        sys.exit(NOT_CONFIGURED_EXIT_CODE)
    exports = [
        f"export OBSERVABILITY_INGEST_URL={shlex.quote('https://' + _telemetry_hostname_for_tier(tier))}",
        f"export OBSERVABILITY_INGEST_CREDENTIAL={shlex.quote(credential)}",
    ]
    _write_private_file(work_dir / "collector.env", "\n".join(exports))


def _store_credentials(tier: str) -> None:
    report = json.loads(sys.stdin.read())
    credential_by_sender = report["credential_by_sender"]
    newly_minted = {
        _INGEST_CREDENTIAL_KEY_BY_SENDER[sender_class.lower()]: entry["authorization_header_value"]
        for sender_class, entry in credential_by_sender.items()
        if entry["is_newly_minted"]
    }
    if not newly_minted:
        logger.info("No newly minted credentials to store for tier {}", tier)
        return
    write_vault_kv(VaultPath(f"secrets/minds/{tier}/observability"), newly_minted)
    logger.info("Stored {} newly minted ingest credential(s) in Vault for tier {}", len(newly_minted), tier)


_USAGE: Final[str] = (
    "usage: provision_observability_config.py resolve <tier> <work_dir>\n"
    "       provision_observability_config.py collector-env <tier> <sender> <work_dir>\n"
    "       provision_observability_config.py store-credentials <tier>"
)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(_USAGE)
    mode = sys.argv[1]
    match mode:
        case "resolve":
            if len(sys.argv) < 4:
                raise SystemExit(_USAGE)
            _resolve(sys.argv[2], Path(sys.argv[3]))
        case "collector-env":
            if len(sys.argv) < 5:
                raise SystemExit(_USAGE)
            _collector_env(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
        case "store-credentials":
            if len(sys.argv) < 3:
                raise SystemExit(_USAGE)
            _store_credentials(sys.argv[2])
        case _:
            raise SystemExit(f"unknown mode {mode!r}; expected resolve | collector-env | store-credentials")


if __name__ == "__main__":
    main()
