"""Resolve everything `just provision-dev-relay` needs into a temp work dir.

Derives the per-env relay coordinates (region label = env name as a DNS
label, content domain from the tier's deploy.toml, plugin-auth URL from the
activated env's client.toml) and pulls the relay SSH keypair plus the
OVH / Cloudflare credentials and the frps plugin secret from Vault. Writes
into the given work dir:

- ``relay_key`` / ``relay_key.pub`` -- the tier's relay SSH keypair (0600).
- ``relay.env`` -- shell-sourceable exports (OVH_*, CLOUDFLARE_*,
  MINDS_ADMIN_KEY, FRPS_AUTH_SECRET).
- ``params.json`` -- ``{region, content_domain, plugin_auth_url, connector_url}``.

Run from the repo root via ``uv run python scripts/provision_dev_relay_config.py``
(the workspace venv provides the imbue.minds imports).
"""

import json
import sys
import tomllib
from pathlib import Path

from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import admin_key_from_supertokens_secret
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds_admin.envs.provisioning import relay_region_for_env


def _shell_quoted(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def main() -> None:
    env_name, tier, config_path, work_dir_arg = sys.argv[1:5]
    work_dir = Path(work_dir_arg)
    region = relay_region_for_env(DevEnvName(env_name))
    deploy_toml = Path("apps/minds/imbue/minds/config/envs") / tier / "deploy.toml"
    with deploy_toml.open("rb") as handle:
        content_domain = tomllib.load(handle)["cloudflare_domain"]
    with Path(config_path).open("rb") as handle:
        connector_url = str(tomllib.load(handle)["connector_url"]).rstrip("/")

    # Pull the Vault entries the relay bring-up needs (supertokens carries the
    # MINDS_ADMIN_KEY that authenticates the fleet-inventory registration).
    vault_prefix = f"secrets/minds/{tier}"
    sharing = read_vault_kv(VaultPath(f"{vault_prefix}/sharing"))
    relay_ssh = read_vault_kv(VaultPath(f"{vault_prefix}/relay-ssh"))
    ovh = read_vault_kv(VaultPath(f"{vault_prefix}/ovh"))
    cloudflare = read_vault_kv(VaultPath(f"{vault_prefix}/cloudflare"))
    supertokens = read_vault_kv(VaultPath(f"{vault_prefix}/supertokens"))

    # The relay SSH keypair, permissions ssh will accept.
    key_path = work_dir / "relay_key"
    key_path.write_text(relay_ssh["RELAY_SSH_PRIVATE_KEY"].rstrip("\n") + "\n")
    key_path.chmod(0o600)
    (work_dir / "relay_key.pub").write_text(relay_ssh["RELAY_SSH_PUBLIC_KEY"].rstrip("\n") + "\n")

    # Shell-consumable creds for the three share-relay steps.
    export_lines = [f"export {key}={_shell_quoted(ovh[key])}" for key in sorted(ovh) if ovh[key]]
    export_lines.append(f"export CLOUDFLARE_API_TOKEN={_shell_quoted(cloudflare['CLOUDFLARE_API_TOKEN'])}")
    export_lines.append(f"export CLOUDFLARE_ZONE_ID={_shell_quoted(cloudflare['CLOUDFLARE_ZONE_ID'])}")
    # The shared resolver handles the deprecated MINDS_PAID_ADMIN_KEY spelling
    # and errors clearly (naming the Vault path) when the key is absent.
    admin_key = admin_key_from_supertokens_secret(supertokens, vault_prefix)
    export_lines.append(f"export MINDS_ADMIN_KEY={_shell_quoted(admin_key)}")
    # The plugin secret travels via the environment (relay.env), never inside
    # the URL: `share-relay deploy` reads FRPS_AUTH_SECRET and renders it as
    # the plugin addr's userinfo.
    export_lines.append(f"export FRPS_AUTH_SECRET={_shell_quoted(sharing['FRPS_AUTH_SECRET'])}")
    (work_dir / "relay.env").write_text("\n".join(export_lines) + "\n")

    plugin_auth_url = f"{connector_url}/frps/auth"
    (work_dir / "params.json").write_text(
        json.dumps(
            {
                "region": region,
                "content_domain": content_domain,
                "plugin_auth_url": plugin_auth_url,
                "connector_url": connector_url,
            }
        )
    )


if __name__ == "__main__":
    main()
