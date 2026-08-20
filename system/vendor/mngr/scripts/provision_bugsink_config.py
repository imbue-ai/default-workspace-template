"""Resolve everything the bugsink justfile recipes need from Vault into a temp work dir.

Two modes (argv[1]):

- ``resolve <tier> <work_dir>`` -- full provisioning inputs: pulls the tier's
  ``bugsink``, ``ovh``, and ``cloudflare`` Vault entries, derives the errors
  hostname from the tier deploy.toml's ``cloudflare_domain``, and writes into
  ``work_dir``:

  - ``ssh_key`` / ``ssh_key.pub`` -- the instance SSH keypair (0600).
  - ``origin.pem`` / ``origin.key`` -- the Cloudflare origin TLS material (0600).
  - ``bugsink.env`` -- shell-sourceable exports (OVH_*, CLOUDFLARE_*,
    BUGSINK_* incl. the origin-file paths).
  - ``params.json`` -- ``{tier, errors_hostname}``.

- ``store-dsns <tier>`` -- reads ``observability bugsink provision-projects``
  JSON on stdin and writes the project DSNs into the tier's ``sentry`` Vault
  entry (for dev, ALSO the ci tier's twin entry -- ci shares the dev
  instance but has its own Vault prefix, and without the twin entry ci
  deploys would silently never report) plus the minted ``BUGSINK_API_TOKEN``
  into the tier's ``bugsink`` entry.

Run from the repo root via ``uv run python scripts/provision_bugsink_config.py``
(the workspace venv provides the imbue.minds imports).
"""

import json
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds.envs.vault_reader import write_vault_kv

_USAGE: Final[str] = (
    "usage: provision_bugsink_config.py resolve <tier> <work_dir>\n"
    "       provision_bugsink_config.py store-dsns <tier>"
)


def _errors_hostname_for_tier(tier: str) -> str:
    deploy_toml = Path("apps/minds/imbue/minds/config/envs") / tier / "deploy.toml"
    with deploy_toml.open("rb") as handle:
        cloudflare_domain = tomllib.load(handle)["cloudflare_domain"]
    return f"errors.{cloudflare_domain}"


def _write_private_file(path: Path, content: str) -> None:
    path.write_text(content.rstrip("\n") + "\n")
    path.chmod(0o600)


def _resolve(tier: str, work_dir: Path) -> None:
    vault_prefix = f"secrets/minds/{tier}"
    bugsink = read_vault_kv(VaultPath(f"{vault_prefix}/bugsink"))
    ovh = read_vault_kv(VaultPath(f"{vault_prefix}/ovh"))
    cloudflare = read_vault_kv(VaultPath(f"{vault_prefix}/cloudflare"))
    errors_hostname = _errors_hostname_for_tier(tier)

    # Multi-line material goes to owner-only files; everything else is
    # exported for the observability CLI to read from the environment.
    _write_private_file(work_dir / "ssh_key", bugsink["BUGSINK_SSH_PRIVATE_KEY"])
    _write_private_file(work_dir / "ssh_key.pub", bugsink["BUGSINK_SSH_PUBLIC_KEY"])
    _write_private_file(work_dir / "origin.pem", bugsink["BUGSINK_ORIGIN_TLS_CERT"])
    _write_private_file(work_dir / "origin.key", bugsink["BUGSINK_ORIGIN_TLS_KEY"])

    export_by_name = {
        "CLOUDFLARE_API_TOKEN": cloudflare["CLOUDFLARE_API_TOKEN"],
        "CLOUDFLARE_ZONE_ID": cloudflare["CLOUDFLARE_ZONE_ID"],
        "BUGSINK_SECRET_KEY": bugsink["SECRET_KEY"],
        "BUGSINK_DATABASE_URL": bugsink["DATABASE_URL"],
        "BUGSINK_CREATE_SUPERUSER": bugsink["CREATE_SUPERUSER"],
        "BUGSINK_ORIGIN_TLS_CERT_FILE": str(work_dir / "origin.pem"),
        "BUGSINK_ORIGIN_TLS_KEY_FILE": str(work_dir / "origin.key"),
    }
    ovh_exports = [f"export {key}={shlex.quote(ovh[key])}" for key in sorted(ovh) if ovh[key]]
    other_exports = [f"export {name}={shlex.quote(value)}" for name, value in export_by_name.items()]
    _write_private_file(work_dir / "bugsink.env", "\n".join(ovh_exports + other_exports))

    (work_dir / "params.json").write_text(json.dumps({"tier": tier, "errors_hostname": errors_hostname}))


def _store_dsns(tier: str) -> None:
    report = json.loads(sys.stdin.read())
    dsn_by_vault_key = report["dsn_by_vault_key"]
    api_token = report["api_token"]

    sentry_vault_paths = [VaultPath(f"secrets/minds/{tier}/sentry")]
    if tier == "dev":
        sentry_vault_paths.append(VaultPath("secrets/minds/ci/sentry"))
    for vault_path in sentry_vault_paths:
        write_vault_kv(vault_path, dsn_by_vault_key)
        logger.info("Stored {} DSN(s) in Vault at {}", len(dsn_by_vault_key), str(vault_path))
    write_vault_kv(VaultPath(f"secrets/minds/{tier}/bugsink"), {"BUGSINK_API_TOKEN": api_token})
    logger.info("Stored BUGSINK_API_TOKEN in Vault for tier {}", tier)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(_USAGE)
    mode = sys.argv[1]
    match mode:
        case "resolve":
            if len(sys.argv) < 4:
                raise SystemExit(_USAGE)
            _resolve(sys.argv[2], Path(sys.argv[3]))
        case "store-dsns":
            if len(sys.argv) < 3:
                raise SystemExit(_USAGE)
            _store_dsns(sys.argv[2])
        case _:
            raise SystemExit(f"unknown mode {mode!r}; expected resolve | store-dsns")


if __name__ == "__main__":
    main()
