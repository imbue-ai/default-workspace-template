#!/usr/bin/env python3
"""Provision one environment's Cloudflare R2 hosting, for one kind of artifact.

Two kinds exist, chosen with ``--kind``, and each gets its own bucket, its own
hostname, and its own bucket-scoped credential per environment:

  * ``lima-images`` (the default) -- the pre-baked Lima image chunk store that
    ``lima_image/publish.py`` uploads to.
  * ``update-feed`` -- the release-channel manifests that
    ``release_channel/publish.py`` publishes. It hosts no binaries; those
    stay on ToDesktop's CDN and the manifests point at them.

Separate buckets are the point: a publish of one kind can never overwrite the
other, and the credential minted for one is ``AccessDenied`` against the other.

This is the one-time setup that must run once per environment and kind before
anything can be published. It is idempotent: re-running against an
already-provisioned environment reports what exists and changes nothing, so it is
safe to run against production after running it against dev.

It performs exactly three things:

  1. Creates the bucket (``minds-lima-images-<env>`` or
     ``minds-update-feed-<env>``).
  2. Attaches a custom domain to it. For images this is required, not a
     preference: a client download fetches tens of thousands of chunks, and the
     managed ``r2.dev`` origin is rate-limited, so an extract served from it dies
     partway through with ``429`` and the image never assembles. The update feed
     needs one for a different reason -- its hostname is compiled into every
     shipped binary and can never change without stranding the installs that
     already carry it.
  3. Mints an R2 API token scoped to *that one bucket*, and prints the S3
     credentials the publisher needs. The account-wide token this script runs
     with is never what publishes; the operator who publishes only ever holds a
     bucket-scoped credential.

The environment name is a full environment, not a tier: ``production``,
``staging``, or a per-developer dev environment such as ``dev-weishi``. Each gets
its own bucket and hostname, so one developer's republish cannot overwrite
another's or production's. (The module name says tier; the argument is an
environment.)

Nothing here is a runtime secret: the app fetches a public URL, and the
credentials below are only ever used by an operator at publish time. What the app
checks on top of that differs by kind -- an image is verified against the public
minisign key named in the tier's ``client.toml``, while the update feed names no
such key, because the manifest it serves carries the sha512 of an artifact
ToDesktop signed and notarized.

Reads ``CLOUDFLARE_API_TOKEN`` / ``CLOUDFLARE_ACCOUNT_ID`` / ``CLOUDFLARE_ZONE_ID``
/ ``CLOUDFLARE_DOMAIN`` -- i.e. the environment's existing Vault ``cloudflare``
entry:

    export VAULT_ADDR=... VAULT_NAMESPACE=admin
    for key in CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_ZONE_ID CLOUDFLARE_DOMAIN; do
      export $key=$(vault kv get -mount=secrets -field=value minds/<tier>/cloudflare/$key)
    done
    uv run python scripts/r2/setup_tier.py --env production
"""

import hashlib
import os
import sys
from dataclasses import dataclass
from typing import Final

import click
import httpx

_API_ROOT = "https://api.cloudflare.com/client/v4"


@dataclass(frozen=True)
class BucketKind:
    """One class of artifact hosted in R2, and the config key that points at it."""

    name: str
    bucket_prefix: str
    hostname_prefix: str
    client_config_key: str
    # Whether production serves the bare, unsuffixed hostname. True for anything
    # whose URL is compiled into a shipped binary: that host can never change
    # without stranding installs that already have it, so it should not carry an
    # environment name it will never shed.
    is_production_hostname_bare: bool = False
    # The client.toml key naming the minisign public key this bucket's artifacts
    # are verified against, or None when it holds nothing separately signed.
    minisign_public_key_config_key: str | None = None


LIMA_IMAGES = BucketKind(
    name="lima-images",
    bucket_prefix="minds-lima-images",
    hostname_prefix="lima-images",
    client_config_key="lima_image_base_url",
    minisign_public_key_config_key="lima_image_minisign_public_key",
)
UPDATE_FEED = BucketKind(
    name="update-feed",
    bucket_prefix="minds-update-feed",
    hostname_prefix="updates",
    client_config_key="update_feed_base_url",
    is_production_hostname_bare=True,
)

# What the bucket holds, keyed by the `--kind` value. Each kind gets its own
# bucket and hostname per environment, so a publish of one can never overwrite
# the other, and the bucket-scoped token minted for one cannot touch the other.
_KINDS: Final[dict[str, BucketKind]] = {kind.name: kind for kind in (LIMA_IMAGES, UPDATE_FEED)}

# Cloudflare permission groups, scoped to a single bucket rather than the whole
# account, and the same pair for either kind. Both publishers read before they
# write, so a write-only token serves neither: `lima_image/publish.py` probes
# each chunk to skip the ones already there, and `release_channel/publish.py`
# reads `<channel>-mac.yml` out of the bucket to check the move is not
# backwards.
_R2_BUCKET_ITEM_READ = "6a018a9f2fc74eb6b293b0c548f38b39"
_R2_BUCKET_ITEM_WRITE = "2efd5506f9c8494dacb1fa10a3e7d5b6"

_DEFAULT_JURISDICTION = "default"


@dataclass(frozen=True)
class CloudflareEnv:
    """The four values an environment's Vault `cloudflare` entry provides."""

    api_token: str
    account_id: str
    zone_id: str
    domain: str


def _read_env() -> CloudflareEnv:
    missing = [
        name
        for name in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ZONE_ID", "CLOUDFLARE_DOMAIN")
        if not os.environ.get(name)
    ]
    if missing:
        raise click.ClickException(f"Missing required environment variable(s): {', '.join(missing)}")
    return CloudflareEnv(
        api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        zone_id=os.environ["CLOUDFLARE_ZONE_ID"],
        domain=os.environ["CLOUDFLARE_DOMAIN"],
    )


class CloudflareClient:
    """The slice of Cloudflare's API this setup needs, with errors surfaced rather than swallowed."""

    def __init__(self, env: CloudflareEnv, client: httpx.Client | None = None) -> None:
        self._env = env
        self._client = client if client is not None else httpx.Client(timeout=30.0)

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        response = self._client.request(
            method,
            f"{_API_ROOT}{path}",
            headers={"Authorization": f"Bearer {self._env.api_token}"},
            json=payload,
        )
        body = response.json()
        if not body.get("success"):
            raise click.ClickException(f"Cloudflare {method} {path} failed: {body.get('errors')}")
        return body.get("result") or {}

    def bucket_exists(self, bucket: str) -> bool:
        # Only a genuine 404 means the bucket is absent. Reading any other failure as
        # "absent" would turn a token that cannot list buckets into a confusing
        # "create failed" further down, instead of naming the permission problem here.
        response = self._client.get(
            f"{_API_ROOT}/accounts/{self._env.account_id}/r2/buckets/{bucket}",
            headers={"Authorization": f"Bearer {self._env.api_token}"},
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return False
        if response.is_success:
            return True
        raise click.ClickException(
            f"Cloudflare GET r2/buckets/{bucket} failed ({response.status_code}): {response.text}"
        )

    def create_bucket(self, bucket: str) -> None:
        self._call("POST", f"/accounts/{self._env.account_id}/r2/buckets", {"name": bucket})

    def custom_domains(self, bucket: str) -> list[dict]:
        result = self._call("GET", f"/accounts/{self._env.account_id}/r2/buckets/{bucket}/domains/custom")
        return result.get("domains") or []

    def attach_custom_domain(self, bucket: str, hostname: str) -> None:
        self._call(
            "POST",
            f"/accounts/{self._env.account_id}/r2/buckets/{bucket}/domains/custom",
            {"domain": hostname, "zoneId": self._env.zone_id, "enabled": True, "minTLS": "1.2"},
        )

    def create_bucket_scoped_r2_token(self, bucket: str, name: str) -> tuple[str, str]:
        """Mint a token that can only read/write objects in ``bucket``; return (token_id, token_value)."""
        resource = f"com.cloudflare.edge.r2.bucket.{self._env.account_id}_{_DEFAULT_JURISDICTION}_{bucket}"
        result = self._call(
            "POST",
            f"/accounts/{self._env.account_id}/tokens",
            {
                "name": name,
                "policies": [
                    {
                        "effect": "allow",
                        "permission_groups": [{"id": _R2_BUCKET_ITEM_READ}, {"id": _R2_BUCKET_ITEM_WRITE}],
                        "resources": {resource: "*"},
                    }
                ],
            },
        )
        return result["id"], result["value"]


def bucket_name(env_name: str, kind: BucketKind = LIMA_IMAGES) -> str:
    """e.g. minds-lima-images-dev-weishi, or minds-update-feed-production"""
    return f"{kind.bucket_prefix}-{env_name}"


def default_hostname(env_name: str, domain: str, kind: BucketKind = LIMA_IMAGES) -> str:
    """e.g. lima-images-production.minds.example, or updates.minds.example"""
    if kind.is_production_hostname_bare and env_name == "production":
        return f"{kind.hostname_prefix}.{domain}"
    return f"{kind.hostname_prefix}-{env_name}.{domain}"


def r2_s3_secret_access_key(token_value: str) -> str:
    """R2 derives an S3 secret access key as the SHA-256 of the API token's value."""
    return hashlib.sha256(token_value.encode()).hexdigest()


@click.command()
@click.option(
    "--env",
    "env_name",
    required=True,
    help="Environment to provision: production, staging, or a dev env such as dev-weishi",
)
@click.option(
    "--kind",
    "kind_name",
    type=click.Choice(sorted(_KINDS)),
    default=LIMA_IMAGES.name,
    show_default=True,
    help="What the bucket holds: pre-baked Lima images, or the release-channel manifests",
)
@click.option("--hostname", default=None, help="Custom domain to serve from (default: derived)")
@click.option("--mint-token/--no-mint-token", default=True, help="Mint a bucket-scoped R2 token for publishing")
@click.option("--dry-run", is_flag=True, help="Report what would change without changing anything")
def main(env_name: str, kind_name: str, hostname: str | None, mint_token: bool, dry_run: bool) -> None:
    env = _read_env()
    kind = _KINDS[kind_name]
    bucket = bucket_name(env_name, kind)
    resolved_hostname = hostname if hostname is not None else default_hostname(env_name, env.domain, kind)
    client = CloudflareClient(env)

    click.echo(f"Environment: {env_name} ({kind.name})")
    click.echo(f"  bucket:   {bucket}")
    click.echo(f"  hostname: {resolved_hostname}")
    click.echo("")

    bucket_already_exists = client.bucket_exists(bucket)
    if bucket_already_exists:
        click.echo(f"[ok]   bucket {bucket} already exists")
    elif dry_run:
        click.echo(f"[plan] would create bucket {bucket}")
    else:
        client.create_bucket(bucket)
        click.echo(f"[new]  created bucket {bucket}")

    # Listing domains on a bucket that does not exist yet 404s, so only ask once it does.
    # A dry run still asks, so it reports what would actually change rather than assuming
    # the domain is missing and always claiming it would attach one.
    attached = [domain["domain"] for domain in client.custom_domains(bucket)] if bucket_already_exists else []
    if resolved_hostname in attached:
        click.echo(f"[ok]   custom domain {resolved_hostname} already attached")
    elif dry_run:
        click.echo(f"[plan] would attach custom domain {resolved_hostname} (zone {env.zone_id})")
    else:
        client.attach_custom_domain(bucket, resolved_hostname)
        click.echo(f"[new]  attached custom domain {resolved_hostname} (DNS + cert take a minute to go active)")

    if dry_run:
        if mint_token:
            click.echo("[plan] would mint a bucket-scoped R2 token")
        return

    click.echo("")
    if mint_token:
        token_id, token_value = client.create_bucket_scoped_r2_token(bucket, f"{bucket}-publish")
        click.echo("Publish credentials (scoped to this bucket only; store them, they are shown once):")
        click.echo("")
        click.echo(f"  export R2_ACCOUNT_ID={env.account_id}")
        click.echo(f"  export R2_ACCESS_KEY_ID={token_id}")
        click.echo(f"  export R2_SECRET_ACCESS_KEY={r2_s3_secret_access_key(token_value)}")
        click.echo("")

    click.echo(f"Commit into the tier's client.toml (public values), once {resolved_hostname} is live:")
    click.echo("")
    click.echo(f'  {kind.client_config_key} = "https://{resolved_hostname}"')
    if kind.minisign_public_key_config_key is not None:
        click.echo(f'  {kind.minisign_public_key_config_key} = "RW..."   # line 2 of the tier\'s minisign .pub')


if __name__ == "__main__":
    sys.exit(main())
