"""Test utilities for remote_service_connector."""

import base64
import contextlib
import json
import re
import secrets
import uuid
from collections.abc import Iterator
from collections.abc import MutableMapping
from collections.abc import Set as AbstractSet
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final
from urllib.parse import quote
from uuid import UUID

# Note: psycopg2.errors is reachable through the base import, matching app.py;
# an explicit ``import psycopg2.errors`` makes ty resolve the module and then
# reject its dynamically-generated members (UniqueViolation) as unknown.
import paramiko
import psycopg2
import pytest
from cachetools.keys import hashkey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.testclient import TestClient
from supertokens_python.recipe.emailpassword.interfaces import ConsumePasswordResetTokenOkResult
from supertokens_python.recipe.emailpassword.interfaces import EmailAlreadyExistsError
from supertokens_python.recipe.emailpassword.interfaces import SignInOkResult as EPSignInOkResult
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult
from supertokens_python.recipe.emailpassword.interfaces import UpdateEmailOrPasswordOkResult
from supertokens_python.recipe.emailpassword.interfaces import WrongCredentialsError
from supertokens_python.recipe.emailverification.interfaces import (
    CreateEmailVerificationTokenEmailAlreadyVerifiedError,
)
from supertokens_python.recipe.emailverification.interfaces import CreateEmailVerificationTokenOkResult
from supertokens_python.recipe.emailverification.interfaces import VerifyEmailUsingTokenOkResult
from supertokens_python.recipe.emailverification.types import EmailVerificationUser
from supertokens_python.recipe.thirdparty.interfaces import ManuallyCreateOrUpdateUserOkResult
from supertokens_python.recipe.thirdparty.provider import RedirectUriInfo
from supertokens_python.recipe.thirdparty.types import RawUserInfoFromProvider
from supertokens_python.recipe.thirdparty.types import ThirdPartyInfo
from supertokens_python.recipe.thirdparty.types import UserInfo
from supertokens_python.recipe.thirdparty.types import UserInfoEmail
from supertokens_python.recipe.webauthn.types.base import WebauthnInfo
from supertokens_python.types import LoginMethod
from supertokens_python.types import RecipeUserId
from supertokens_python.types import User
from supertokens_python.types.base import AccountInfoInput

import imbue.remote_service_connector.accounts as accounts_module
import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.app as app_mod
import imbue.remote_service_connector.attribution as attribution_module
import imbue.remote_service_connector.auth as auth_mod
import imbue.remote_service_connector.auth_proxy as auth_proxy_module
import imbue.remote_service_connector.cloudflare as cloudflare_mod
import imbue.remote_service_connector.db as db_mod
import imbue.remote_service_connector.entitlements as entitlements_mod
import imbue.remote_service_connector.hosts as hosts_module
import imbue.remote_service_connector.litellm_client as litellm_client_mod
import imbue.remote_service_connector.r2.stores as r2_stores_mod
import imbue.remote_service_connector.share_broker as share_broker_module
import imbue.remote_service_connector.signup_hardening as signup_hardening_module
import imbue.remote_service_connector.stop_start as stop_start_module
import imbue.remote_service_connector.storage as connector_storage_module
import imbue.remote_service_connector.suspension as suspension_module
import imbue.remote_service_connector.suspension_admin as suspension_admin_module
import imbue.remote_service_connector.sync as sync_mod
from imbue.remote_service_connector.auth import UserAuth
from imbue.remote_service_connector.auth import derive_user_id_prefix
from imbue.remote_service_connector.box_scripts import CLEANUP_DELETE_FAILED_MARKER
from imbue.remote_service_connector.cloudflare import CloudflareCtx
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import MissingStorageConfigError
from imbue.remote_service_connector.errors import PoolHostCleanupError
from imbue.remote_service_connector.errors import R2BucketNotEmptyError
from imbue.remote_service_connector.errors import R2BucketNotFoundError

# Imported from the production module so the fake's tuple order can never
# drift from what PostgresShareStore SELECTs (same rationale as
# _WORKSPACE_RECORD_COLUMNS below).
from imbue.remote_service_connector.shares import _SHARE_COLUMN_NAMES
from imbue.remote_service_connector.sync import SyncRecordFormatTooNewError
from imbue.remote_service_connector.sync import SyncRevisionConflictError
from imbue.remote_service_connector.sync import UPDATABLE_RECORD_COLUMNS
from imbue.remote_service_connector.sync import _WORKSPACE_RECORD_COLUMNS
from imbue.remote_service_connector.web import web_app

# Shared RSA signing key for the accounts-surface OAuth tests: it mints the
# BROKER_JWT_SIGNING_KEY_PEM env value and verifies the signed OAuth state.
# Generated once here so each test module does not pay its own keygen.
TEST_OAUTH_SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_OAUTH_SIGNING_KEY_PEM = TEST_OAUTH_SIGNING_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")


class FakeCloudflareOps:
    """In-memory fake implementing the CloudflareOps protocol for testing."""

    def __init__(self) -> None:
        # R2 state
        self.account_id = "test-account"
        self.buckets: dict[str, dict[str, Any]] = {}
        # Per-bucket object lists; tests append to mark a bucket non-empty.
        self.bucket_objects: dict[str, list[str]] = {}
        self.account_tokens: dict[str, dict[str, Any]] = {}
        self._next_r2_token_id = 1
        # Stored bytes per bucket, served by the per-bucket REST usage fake
        # (and by the GraphQL sweep fake unless the knob below diverges them);
        # tests set entries directly.
        self.usage_bytes_by_bucket: dict[str, int] = {}
        # When set, the GraphQL sweep fake serves THIS map instead of
        # usage_bytes_by_bucket, so tests can model the analytics window peak
        # diverging from live REST usage (the confirm-before-downgrade path).
        self.graphql_usage_bytes_by_bucket: dict[str, int] | None = None
        # Failure-injection knobs: the next delete_bucket_token call raises,
        # exercising the sweep revoke-retry paths. While
        # fail_bucket_usage_reads is set, every per-bucket REST usage read
        # raises (the sweep/gate fail-open paths).
        self.fail_next_delete_bucket_token = False
        self.fail_bucket_usage_reads = False

    def create_bucket(self, name: str) -> dict[str, Any]:
        if name in self.buckets:
            raise CloudflareApiError(status_code=400, errors=[{"message": f"bucket already exists: {name}"}])
        bucket = {"name": name}
        self.buckets[name] = bucket
        self.bucket_objects.setdefault(name, [])
        return bucket

    def list_buckets(self, name_contains: str = "") -> list[dict[str, Any]]:
        return [bucket for name, bucket in self.buckets.items() if name_contains in name]

    def delete_bucket(self, name: str) -> None:
        if name not in self.buckets:
            raise R2BucketNotFoundError(name)
        if self.bucket_objects.get(name):
            raise R2BucketNotEmptyError(name)
        del self.buckets[name]
        self.bucket_objects.pop(name, None)

    def list_bucket_object_keys(self, bucket_name: str, limit: int) -> list[str]:
        if bucket_name not in self.buckets:
            raise R2BucketNotFoundError(bucket_name)
        return list(self.bucket_objects.get(bucket_name, []))[:limit]

    def delete_bucket_object(self, bucket_name: str, key: str) -> None:
        objects = self.bucket_objects.get(bucket_name)
        if objects is not None and key in objects:
            objects.remove(key)

    def create_bucket_token(self, bucket_name: str, access: str, token_name: str) -> dict[str, Any]:
        token_id = f"r2tok-{self._next_r2_token_id}"
        self._next_r2_token_id += 1
        self.account_tokens[token_id] = {
            "id": token_id,
            "name": token_name,
            "bucket_name": bucket_name,
            "access": access,
        }
        return {"id": token_id, "value": f"token-value-{token_id}"}

    def delete_bucket_token(self, token_id: str) -> None:
        if self.fail_next_delete_bucket_token:
            self.fail_next_delete_bucket_token = False
            raise CloudflareApiError(status_code=500, errors=[{"message": "simulated token revoke failure"}])
        self.account_tokens.pop(token_id, None)

    def update_bucket_token_access(self, token_id: str, bucket_name: str, access: str, token_name: str) -> None:
        token = self.account_tokens.get(token_id)
        if token is None:
            raise CloudflareApiError(status_code=404, errors=[{"message": f"token not found: {token_id}"}])
        token["access"] = access
        token["bucket_name"] = bucket_name
        token["name"] = token_name

    def set_bucket_token_status(
        self, token_id: str, bucket_name: str, access: str, token_name: str, status: str
    ) -> None:
        token = self.account_tokens.get(token_id)
        if token is None:
            raise CloudflareApiError(status_code=404, errors=[{"message": f"token not found: {token_id}"}])
        token["status"] = status
        token["access"] = access
        token["bucket_name"] = bucket_name
        token["name"] = token_name

    def roll_bucket_token_value(self, token_id: str) -> dict[str, Any]:
        token = self.account_tokens.get(token_id)
        if token is None:
            raise CloudflareApiError(status_code=404, errors=[{"message": f"token not found: {token_id}"}])
        roll_count = int(token.get("roll_count", 0)) + 1
        token["roll_count"] = roll_count
        return {"value": f"token-value-{token_id}-roll{roll_count}"}

    def get_bucket_usage_bytes(self, bucket_name: str) -> int:
        if self.fail_bucket_usage_reads:
            raise CloudflareApiError(status_code=500, errors=[{"message": "simulated usage read failure"}])
        return int(self.usage_bytes_by_bucket.get(bucket_name, 0))

    def query_r2_storage_by_bucket(self) -> dict[str, int]:
        if self.graphql_usage_bytes_by_bucket is not None:
            return dict(self.graphql_usage_bytes_by_bucket)
        return dict(self.usage_bytes_by_bucket)


class InMemoryKeyStore:
    """In-memory KeyStore implementation for testing the bucket-key endpoints."""

    def __init__(self) -> None:
        # access_key_id -> stored row dict
        self.keys_by_access_key_id: dict[str, dict[str, Any]] = {}
        self._created_counter = 0

    def add_key(
        self, access_key_id: str, owner_user_id: str, bucket_name: str, access: str, alias: str | None
    ) -> None:
        self._created_counter += 1
        self.keys_by_access_key_id[access_key_id] = {
            "access_key_id": access_key_id,
            "owner_user_id": owner_user_id,
            "bucket_name": bucket_name,
            "access": access,
            "alias": alias,
            "created_at": f"2026-01-01T00:00:{self._created_counter:02d}+00:00",
            "enforced_access": None,
            "suspension_access": None,
        }

    def list_keys(self, owner_user_id: str, bucket_name: str | None = None) -> list[dict[str, Any]]:
        rows = [r for r in self.keys_by_access_key_id.values() if r["owner_user_id"] == owner_user_id]
        if bucket_name is not None:
            rows = [r for r in rows if r["bucket_name"] == bucket_name]
        return sorted(rows, key=lambda r: r["created_at"])

    def list_all_keys(self) -> list[dict[str, Any]]:
        return sorted(
            self.keys_by_access_key_id.values(),
            key=lambda r: (r["owner_user_id"], r["bucket_name"], r["created_at"]),
        )

    def get_key(self, access_key_id: str) -> dict[str, Any] | None:
        row = self.keys_by_access_key_id.get(access_key_id)
        return dict(row) if row is not None else None

    def delete_key(self, access_key_id: str) -> None:
        self.keys_by_access_key_id.pop(access_key_id, None)

    def set_enforced_access(self, access_key_id: str, enforced_access: str | None) -> None:
        row = self.keys_by_access_key_id.get(access_key_id)
        if row is not None:
            row["enforced_access"] = enforced_access

    def set_suspension_access(self, access_key_id: str, suspension_access: str | None) -> None:
        row = self.keys_by_access_key_id.get(access_key_id)
        if row is not None:
            row["suspension_access"] = suspension_access

    def delete_keys_for_bucket(self, owner_user_id: str, bucket_name: str) -> list[dict[str, Any]]:
        removed = [
            r
            for r in self.keys_by_access_key_id.values()
            if r["owner_user_id"] == owner_user_id and r["bucket_name"] == bucket_name
        ]
        for row in removed:
            del self.keys_by_access_key_id[row["access_key_id"]]
        return removed


def make_fake_key_store() -> InMemoryKeyStore:
    """Construct an empty in-memory KeyStore for tests."""
    return InMemoryKeyStore()


class FakeCloudflareCtx(CloudflareCtx):
    """CloudflareCtx backed by FakeCloudflareOps for testing."""

    fake: FakeCloudflareOps


def make_fake_cloudflare_ctx() -> FakeCloudflareCtx:
    """Create a FakeCloudflareCtx for testing."""
    fake = FakeCloudflareOps()
    ctx = FakeCloudflareCtx(ops=fake)
    ctx.fake = fake
    return ctx


# ---------------------------------------------------------------------------
# SuperTokens SDK fakes
#
# The remote_service_connector service wraps the SuperTokens SDK behind /auth/*
# endpoints. Exercising those endpoints against a real SuperTokens core is
# slow (Docker) and unreliable in CI, so the tests install the fakes below as
# drop-in replacements for every SDK function the handlers call. The backend
# state (accounts, sessions, reset tokens) lives on a single
# ``FakeSuperTokensBackend`` instance; ``FakeSuperTokensBackend.install_on_app_module``
# swaps the SDK references on ``remote_service_connector.app`` over to methods on
# that instance. Swapping the ``app`` module's bound references (rather than
# the SDK's source module) means handlers see fakes without needing to
# initialize the real SuperTokens SDK, which would fail without a live core.
# ---------------------------------------------------------------------------


_USER_ID_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _deterministic_user_id(email: str, provider: str) -> str:
    return str(uuid.uuid5(_USER_ID_NAMESPACE, f"{provider}:{email}"))


class FakeAccount:
    """In-memory record for a single SuperTokens account.

    Kept as a plain attribute bag so it can be mutated freely; not part of the
    ``FakeSuperTokensBackend`` public API.
    """

    user_id: str
    email: str
    password: str | None
    is_verified: bool
    provider_id: str
    third_party_user_id: str | None
    display_name: str | None


def _make_account(
    email: str,
    password: str | None,
    provider_id: str,
    third_party_user_id: str | None,
    display_name: str | None,
    is_verified: bool,
) -> FakeAccount:
    account = FakeAccount()
    account.user_id = _deterministic_user_id(email, provider_id)
    account.email = email
    account.password = password
    account.is_verified = is_verified
    account.provider_id = provider_id
    account.third_party_user_id = third_party_user_id
    account.display_name = display_name
    return account


def _build_st_user(account: FakeAccount) -> User:
    """Build a supertokens-python User from a FakeAccount."""
    is_thirdparty = account.provider_id != "emailpassword"
    recipe_id = "thirdparty" if is_thirdparty else "emailpassword"
    third_party_info: ThirdPartyInfo | None = None
    if is_thirdparty and account.third_party_user_id is not None:
        third_party_info = ThirdPartyInfo(
            third_party_user_id=account.third_party_user_id,
            third_party_id=account.provider_id,
        )
    login_method = LoginMethod(
        recipe_id=recipe_id,
        recipe_user_id=account.user_id,
        tenant_ids=["public"],
        email=account.email,
        phone_number=None,
        third_party=third_party_info,
        webauthn=None,
        time_joined=0,
        verified=account.is_verified,
    )
    return User(
        user_id=account.user_id,
        is_primary_user=False,
        tenant_ids=["public"],
        emails=[account.email],
        phone_numbers=[],
        third_party=[],
        webauthn=WebauthnInfo(credential_ids=[]),
        login_methods=[login_method],
        time_joined=0,
    )


class FakeSessionContainer:
    """Minimal SessionContainer stand-in exposing the methods handlers use."""

    access_token: str
    refresh_token: str
    user_id: str
    access_token_payload: dict[str, Any]

    def get_user_id(self) -> str:
        return self.user_id

    def get_all_session_tokens_dangerously(self) -> dict[str, str]:
        return {"accessToken": self.access_token, "refreshToken": self.refresh_token}

    def get_access_token_payload(self) -> dict[str, Any]:
        return self.access_token_payload

    def get_handle(self) -> str:
        # The fake keys sessions by access token, so the token doubles as the
        # session handle the revoke fake resolves.
        return self.access_token


def _make_session(user_id: str) -> FakeSessionContainer:
    session = FakeSessionContainer()
    session.user_id = user_id
    session.access_token = f"at-{secrets.token_hex(8)}"
    session.refresh_token = f"rt-{secrets.token_hex(8)}"
    session.access_token_payload = {}
    return session


class FakeProvider:
    """Stand-in for an OAuth provider exposing the async surface handlers use."""

    provider_id: str
    email: str
    third_party_user_id: str
    display_name: str | None
    is_verified: bool

    async def get_authorisation_redirect_url(
        self,
        redirect_uri_on_provider_dashboard: str,
        user_context: dict[str, Any],
    ) -> Any:
        class _Redirect:
            url_with_query_params: str

        redirect = _Redirect()
        redirect.url_with_query_params = (
            f"https://{self.provider_id}.example.com/auth?redirect_uri={redirect_uri_on_provider_dashboard}&state=s"
        )
        return redirect

    async def exchange_auth_code_for_oauth_tokens(
        self,
        redirect_uri_info: RedirectUriInfo,
        user_context: dict[str, Any],
    ) -> dict[str, str]:
        return {"access_token": "oauth-at"}

    async def get_user_info(
        self,
        oauth_tokens: dict[str, str],
        user_context: dict[str, Any],
    ) -> UserInfo:
        raw = RawUserInfoFromProvider(
            from_id_token_payload=None,
            from_user_info_api={"name": self.display_name} if self.display_name else None,
        )
        return UserInfo(
            third_party_user_id=self.third_party_user_id,
            email=UserInfoEmail(email=self.email, is_verified=self.is_verified),
            raw_user_info_from_provider=raw,
        )


class FakeSuperTokensBackend:
    """In-memory SuperTokens replacement for unit-testing the /auth/* handlers.

    Tracks every piece of server-side state the handlers depend on (accounts,
    sessions, email-verification tokens, password-reset tokens, OAuth provider
    configuration) so the fake can answer any SDK call the handlers make
    without talking to a real SuperTokens core.

    The counters below (``sent_verification_emails``, ``sent_reset_emails``)
    let tests assert that side-effecting SDK calls actually fired, not just
    that the handler returned OK.
    """

    accounts_by_id: dict[str, FakeAccount]
    accounts_by_email: dict[str, FakeAccount]
    sessions_by_access_token: dict[str, FakeSessionContainer]
    sessions_by_refresh_token: dict[str, FakeSessionContainer]
    reset_tokens: dict[str, str]
    verification_tokens: dict[str, tuple[str, str]]
    registered_providers: dict[str, FakeProvider]
    sent_verification_emails: list[tuple[str, str]]
    sent_reset_emails: list[tuple[str, str]]
    # Error-injection hook: if a method name is a key here, the corresponding
    # SDK fake raises the stored exception instead of producing a result. Lets
    # tests exercise the /auth/* SDK-outage code paths through the real handler
    # without patching module-level attributes.
    sdk_errors_by_method: dict[str, Exception]
    # The most recent browser session minted through the accounts-web seam,
    # for the test to plant on its client as the BROWSER_SESSION_COOKIE.
    last_browser_session: "FakeSessionContainer | None"
    # Whether the fake Turnstile verifier accepts tokens (flip to False to
    # exercise the TURNSTILE_FAILED path).
    is_turnstile_passing: bool
    # In-memory device-auth-code store installed onto accounts_web.
    device_code_store: "InMemoryDeviceAuthCodeStore"
    # In-memory attribution store installed onto the attribution module.
    attribution_store: "InMemoryAttributionStore"
    # In-memory entitlements store (pre-seeded with the committed plans),
    # installed onto accounts_web's get_entitlements_store seam so signup
    # plan choices land somewhere assertable.
    entitlements_store: "InMemoryEntitlementsStore"
    # In-memory signup-hardening stores/seams installed onto signup_hardening.
    signup_attempt_store: "InMemorySignupAttemptStore"
    ip_reputation_cache: "InMemoryIpReputationCache"
    ip_reputation_provider: "FakeIpReputationProvider"
    tor_exit_list: "FakeTorExitList"
    # The client IP the fake `_client_ip` seam reports for every request (the
    # test client's real socket peer is not a routable IP). None models an
    # underivable client IP.
    fake_client_ip: str | None
    # Accounts the fake suspension seam reports as suspended -- the login
    # gates consult this instead of the real entitlements DB.
    suspended_user_ids: set[str]

    def install_on_app_module(self, app_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Swap every SuperTokens SDK call site with a fake.

        Driving the patches through a single dict + loop keeps this helper to
        exactly one attribute-patch call no matter how many SDK functions we
        stub, which limits the blast radius on the test-patching ratchet.
        Each SDK name is patched on every connector module that imported it
        (the SDK functions are referenced as module globals at call time, so
        patching the importing module's attribute is what takes effect).
        """
        # If a quota test client already installed an in-memory entitlements
        # store on the seam, adopt it rather than shadowing it: browser-signup
        # writes and quota-endpoint reads must resolve one store, or
        # cross-flow assertions lie.
        already_installed_store = entitlements_mod.get_entitlements_store()
        if isinstance(already_installed_store, InMemoryEntitlementsStore):
            self.entitlements_store = already_installed_store
        fakes: dict[str, Any] = {
            "ep_sign_up": self.sign_up,
            "ep_sign_in": self.sign_in,
            "is_email_verified": self.is_email_verified,
            "send_email_verification_email": self.send_email_verification_email,
            "create_new_session_without_request_response": self.create_new_session,
            "refresh_session_without_request_response": self.refresh_session,
            "revoke_all_sessions_for_user": self.revoke_all_sessions_for_user,
            "revoke_session": self.revoke_session,
            "get_user": self.get_user,
            "get_session_without_request_response": self.get_session,
            "list_users_by_account_info": self.list_users_by_account_info,
            "send_reset_password_email": self.send_reset_password_email,
            "consume_password_reset_token": self.consume_password_reset_token,
            "update_email_or_password": self.update_email_or_password,
            "create_email_verification_token": self.create_email_verification_token,
            "verify_email_using_token": self.verify_email_using_token,
            "get_accounts_oauth_provider": self.get_accounts_oauth_provider,
            "manually_create_or_update_user": self.manually_create_or_update_user,
            "_sdk_create_browser_session": self.sdk_create_browser_session,
            "_sdk_get_browser_session": self.sdk_get_browser_session,
            "delete_user": self.sdk_delete_user,
            # Not SuperTokens seams, but accounts-surface test plumbing that
            # rides the same single-loop install: the Turnstile verifier
            # (driven by ``is_turnstile_passing``), the in-memory
            # device-auth-code / attribution / signup-hardening stores, and
            # the client-IP seam (driven by ``fake_client_ip``).
            "_verify_turnstile_token": self.verify_turnstile_token,
            "_device_code_store": self.device_code_store,
            "_attribution_store": self.attribution_store,
            "get_entitlements_store": lambda: self.entitlements_store,
            "_signup_attempt_store": self.signup_attempt_store,
            "_ip_reputation_cache": self.ip_reputation_cache,
            "_ip_reputation_provider": self.ip_reputation_provider,
            "_tor_exit_list": self.tor_exit_list,
            "_client_ip": self.client_ip,
            "is_user_suspended": self.is_user_suspended_check,
        }
        target_modules = [
            app_mod,
            auth_mod,
            auth_proxy_module,
            accounts_module,
            share_broker_module,
            accounts_web_module,
            attribution_module,
            signup_hardening_module,
            suspension_module,
            suspension_admin_module,
            # accounts_web's signup plan recorder resolves the entitlements
            # store through this module (the runtime-seam convention).
            entitlements_mod,
        ]
        for name, fake in fakes.items():
            matching_modules = [module for module in target_modules if hasattr(module, name)]
            # A fake that matches no module would leave the real SDK function in
            # place, so fail loudly instead of silently skipping it.
            assert matching_modules, f"no connector module imports SuperTokens SDK name {name!r}"
            for target_module in matching_modules:
                monkeypatch.setattr(target_module, name, fake)
        # Verification-email cooldown state is keyed by user_id, and the fake
        # derives user_ids deterministically from the email -- so without a
        # reset, a test that signs up "a@b.com" would suppress verification
        # sends for every later test reusing that address.
        auth_proxy_module._verification_email_sent_at_monotonic_by_user_id.clear()

    def register_provider(
        self,
        provider_id: str,
        *,
        email: str = "oauth@example.com",
        third_party_user_id: str = "tp-user-1",
        display_name: str | None = "OAuth User",
        is_verified: bool = True,
    ) -> None:
        """Register an OAuth provider (returned by ``get_accounts_oauth_provider``; tests reach it via ``registered_providers``)."""
        provider = FakeProvider()
        provider.provider_id = provider_id
        provider.email = email
        provider.third_party_user_id = third_party_user_id
        provider.display_name = display_name
        provider.is_verified = is_verified
        self.registered_providers[provider_id] = provider

    def add_third_party_account(
        self,
        *,
        provider_id: str,
        email: str,
        third_party_user_id: str,
        is_verified: bool = True,
    ) -> str:
        """Seed a third-party account directly, bypassing the auth routes.

        Lets tests set up pre-existing cross-method duplicates (two accounts
        sharing one email) that the one-account-per-email signup guard refuses
        to create through the routes. Returns the new account's user id.
        """
        account = _make_account(
            email=email,
            password=None,
            provider_id=provider_id,
            third_party_user_id=third_party_user_id,
            display_name=None,
            is_verified=is_verified,
        )
        self.accounts_by_id[account.user_id] = account
        self.accounts_by_email.setdefault(email, account)
        return account.user_id

    def mark_email_verified(self, user_id: str) -> None:
        """Force-flip an account to verified (bypassing the token flow)."""
        account = self.accounts_by_id.get(user_id)
        if account is not None:
            account.is_verified = True

    def issue_reset_token(self, user_id: str) -> str:
        """Issue a password-reset token directly, without going through forgot-password."""
        token = f"reset-{secrets.token_hex(8)}"
        self.reset_tokens[token] = user_id
        return token

    def raise_on(self, method_name: str, exc: Exception) -> None:
        """Arrange for the named SDK-fake method to raise ``exc`` on its next call.

        The fake SDK methods check ``sdk_errors_by_method`` at entry; this
        helper lets tests simulate SuperTokens core outages through the real
        handler's try/except blocks without patching module-level attributes.
        """
        self.sdk_errors_by_method[method_name] = exc

    def _raise_if_configured(self, method_name: str) -> None:
        exc = self.sdk_errors_by_method.get(method_name)
        if exc is not None:
            raise exc

    def sign_up(
        self,
        *,
        tenant_id: str,
        email: str,
        password: str,
        user_context: dict[str, Any] | None = None,
    ) -> EPSignUpOkResult | EmailAlreadyExistsError:
        del tenant_id, user_context
        self._raise_if_configured("sign_up")
        # Scope the duplicate check to emailpassword accounts, matching the real
        # recipe: a same-email third-party account does NOT block a password
        # signup (that per-recipe scoping is what makes cross-method duplicates
        # possible, so the fake must permit them for the route-level guard to be
        # the thing under test). Match emails case-insensitively, like the real
        # core (which normalizes emails to lowercase) and like the fake's own
        # ``list_users_by_account_info``.
        has_email_password_account = any(
            existing.provider_id == "emailpassword" and existing.email.lower() == email.lower()
            for existing in self.accounts_by_id.values()
        )
        if has_email_password_account:
            return EmailAlreadyExistsError()
        account = _make_account(
            email=email,
            password=password,
            provider_id="emailpassword",
            third_party_user_id=None,
            display_name=None,
            is_verified=False,
        )
        # Plain assignment (not setdefault): ``sign_in`` resolves passwords
        # through this dict, so the password account must own the email slot
        # even when a same-email third-party account claimed it first.
        self.accounts_by_email[email] = account
        self.accounts_by_id[account.user_id] = account
        user = _build_st_user(account)
        return EPSignUpOkResult(user=user, recipe_user_id=RecipeUserId(account.user_id))

    def sign_in(
        self,
        *,
        tenant_id: str,
        email: str,
        password: str,
        user_context: dict[str, Any] | None = None,
    ) -> EPSignInOkResult | WrongCredentialsError:
        del tenant_id, user_context
        self._raise_if_configured("sign_in")
        account = self.accounts_by_email.get(email)
        if account is None or account.password != password:
            return WrongCredentialsError()
        user = _build_st_user(account)
        return EPSignInOkResult(user=user, recipe_user_id=RecipeUserId(account.user_id))

    def is_email_verified(
        self,
        *,
        recipe_user_id: RecipeUserId,
        email: str,
        user_context: dict[str, Any] | None = None,
    ) -> bool:
        del email, user_context
        account = self.accounts_by_id.get(recipe_user_id.get_as_string())
        return account is not None and account.is_verified

    def send_email_verification_email(
        self,
        *,
        tenant_id: str,
        user_id: str,
        recipe_user_id: RecipeUserId,
        email: str,
        user_context: dict[str, Any] | None = None,
    ) -> None:
        del tenant_id, recipe_user_id, user_context
        self._raise_if_configured("send_email_verification_email")
        token = f"verify-{secrets.token_hex(8)}"
        self.verification_tokens[token] = (user_id, email)
        self.sent_verification_emails.append((user_id, email))

    def create_new_session(
        self,
        *,
        tenant_id: str,
        recipe_user_id: RecipeUserId,
        access_token_payload: dict[str, Any] | None = None,
        session_data_in_database: dict[str, Any] | None = None,
        disable_anti_csrf: bool = False,
        user_context: dict[str, Any] | None = None,
    ) -> FakeSessionContainer:
        del tenant_id, access_token_payload, session_data_in_database, disable_anti_csrf, user_context
        session = _make_session(recipe_user_id.get_as_string())
        self.sessions_by_access_token[session.access_token] = session
        self.sessions_by_refresh_token[session.refresh_token] = session
        return session

    def refresh_session(
        self,
        *,
        refresh_token: str,
        anti_csrf_token: str | None = None,
        disable_anti_csrf: bool = False,
        user_context: dict[str, Any] | None = None,
    ) -> FakeSessionContainer:
        del anti_csrf_token, disable_anti_csrf, user_context
        old = self.sessions_by_refresh_token.get(refresh_token)
        if old is None:
            raise ValueError("Invalid refresh token")
        del self.sessions_by_refresh_token[refresh_token]
        self.sessions_by_access_token.pop(old.access_token, None)
        session = _make_session(old.user_id)
        # Like the real SDK, custom access token payload survives a refresh
        # (the ~30-day cap's started-at stamp must keep its original value).
        session.access_token_payload = dict(old.access_token_payload)
        self.sessions_by_access_token[session.access_token] = session
        self.sessions_by_refresh_token[session.refresh_token] = session
        return session

    def revoke_all_sessions_for_user(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
        revoke_across_all_tenants: bool = True,
        user_context: dict[str, Any] | None = None,
    ) -> list[str]:
        del tenant_id, revoke_across_all_tenants, user_context
        revoked: list[str] = []
        for session in list(self.sessions_by_access_token.values()):
            if session.user_id == user_id:
                revoked.append(session.access_token)
                self.sessions_by_access_token.pop(session.access_token, None)
                self.sessions_by_refresh_token.pop(session.refresh_token, None)
        return revoked

    def revoke_session(
        self,
        session_handle: str,
        user_context: dict[str, Any] | None = None,
    ) -> bool:
        del user_context
        # The fake's session handle IS the access token (see
        # ``FakeSessionContainer.get_handle``).
        session = self.sessions_by_access_token.pop(session_handle, None)
        if session is None:
            return False
        self.sessions_by_refresh_token.pop(session.refresh_token, None)
        return True

    def get_user(self, user_id: str, user_context: dict[str, Any] | None = None) -> User | None:
        del user_context
        account = self.accounts_by_id.get(user_id)
        if account is None:
            return None
        return _build_st_user(account)

    def get_session(
        self,
        *,
        access_token: str,
        anti_csrf_check: bool = False,
        session_required: bool = True,
        check_database: bool | None = None,
        override_global_claim_validators: Any = None,
        user_context: dict[str, Any] | None = None,
    ) -> FakeSessionContainer | None:
        # The fake's session map IS the "database": a revoked session is
        # removed from it, so both stateless and check_database verification
        # collapse to the same lookup here.
        del anti_csrf_check, session_required, check_database, override_global_claim_validators, user_context
        return self.sessions_by_access_token.get(access_token)

    def list_users_by_account_info(
        self,
        *,
        tenant_id: str,
        account_info: AccountInfoInput,
        do_union_of_account_info: bool = False,
        user_context: dict[str, Any] | None = None,
    ) -> list[User]:
        del tenant_id, do_union_of_account_info, user_context
        self._raise_if_configured("list_users_by_account_info")
        if account_info.email is None:
            return []
        # Scan accounts_by_id rather than the one-value-per-email accounts_by_email
        # dict so cross-method duplicates (same email, different login methods) are
        # all reported, the way the real SDK reports them. Match emails
        # case-insensitively, also like the real core (which normalizes emails to
        # lowercase): callers such as the one-account-per-email guard lowercase the
        # email before looking it up.
        email_lower = account_info.email.lower()
        return [
            _build_st_user(account) for account in self.accounts_by_id.values() if account.email.lower() == email_lower
        ]

    def send_reset_password_email(
        self,
        *,
        tenant_id: str,
        user_id: str,
        email: str,
        user_context: dict[str, Any] | None = None,
    ) -> str:
        del tenant_id, user_context
        if user_id not in self.accounts_by_id:
            return "UNKNOWN_USER_ID_ERROR"
        token = f"reset-{secrets.token_hex(8)}"
        self.reset_tokens[token] = user_id
        self.sent_reset_emails.append((user_id, email))
        return "OK"

    def consume_password_reset_token(
        self,
        *,
        tenant_id: str,
        token: str,
        user_context: dict[str, Any] | None = None,
    ) -> ConsumePasswordResetTokenOkResult | Any:
        del tenant_id, user_context
        user_id = self.reset_tokens.pop(token, None)
        if user_id is None:

            class _Invalid:
                status: str = "RESET_PASSWORD_INVALID_TOKEN_ERROR"

            return _Invalid()
        account = self.accounts_by_id[user_id]
        return ConsumePasswordResetTokenOkResult(email=account.email, user_id=user_id)

    def update_email_or_password(
        self,
        *,
        recipe_user_id: RecipeUserId,
        email: str | None = None,
        password: str | None = None,
        apply_password_policy: bool = True,
        tenant_id_for_password_policy: str = "public",
        user_context: dict[str, Any] | None = None,
    ) -> UpdateEmailOrPasswordOkResult:
        del apply_password_policy, tenant_id_for_password_policy, user_context
        account = self.accounts_by_id[recipe_user_id.get_as_string()]
        if email is not None:
            account.email = email
        if password is not None:
            account.password = password
        return UpdateEmailOrPasswordOkResult()

    def create_email_verification_token(
        self,
        *,
        tenant_id: str,
        recipe_user_id: RecipeUserId,
        email: str,
        user_context: dict[str, Any] | None = None,
    ) -> CreateEmailVerificationTokenOkResult | CreateEmailVerificationTokenEmailAlreadyVerifiedError:
        del tenant_id, user_context
        self._raise_if_configured("create_email_verification_token")
        user_id = recipe_user_id.get_as_string()
        account = self.accounts_by_id.get(user_id)
        if account is not None and account.is_verified:
            return CreateEmailVerificationTokenEmailAlreadyVerifiedError()
        token = f"verify-{secrets.token_hex(8)}"
        self.verification_tokens[token] = (user_id, email)
        return CreateEmailVerificationTokenOkResult(token=token)

    def verify_email_using_token(
        self,
        *,
        tenant_id: str,
        token: str,
        attempt_account_linking: bool = True,
        user_context: dict[str, Any] | None = None,
    ) -> VerifyEmailUsingTokenOkResult | Any:
        del tenant_id, attempt_account_linking, user_context
        pair = self.verification_tokens.pop(token, None)
        if pair is None:

            class _Invalid:
                status: str = "EMAIL_VERIFICATION_INVALID_TOKEN_ERROR"

            return _Invalid()
        user_id, email = pair
        account = self.accounts_by_id[user_id]
        account.is_verified = True
        return VerifyEmailUsingTokenOkResult(
            user=EmailVerificationUser(recipe_user_id=RecipeUserId(user_id), email=email),
        )

    def get_accounts_oauth_provider(self) -> FakeProvider | None:
        """Stand-in for the accounts surface's env-driven Google provider: configured iff 'google' is registered."""
        return self.registered_providers.get("google")

    # The browser-session cookie name the fake seams speak. Tests set it on
    # their TestClient after a fake signin (``last_browser_session``); the
    # real SDK middleware owns the equivalent cookies in production.
    BROWSER_SESSION_COOKIE = "st_browser"

    def sdk_create_browser_session(self, request: Any, user_id: str) -> FakeSessionContainer:
        """Fake for ``accounts_web._sdk_create_browser_session``.

        Cannot set response cookies from here (no response object flows
        through the seam), so the created session is exposed as
        ``last_browser_session`` for the test to plant on its client.
        """
        del request
        session = _make_session(user_id)
        # Mirror the real seam's payload (the ~30-day cap is measured against
        # the started-at stamp it carries).
        session.access_token_payload = accounts_web_module._new_browser_session_access_token_payload()
        self.sessions_by_access_token[session.access_token] = session
        self.sessions_by_refresh_token[session.refresh_token] = session
        self.last_browser_session = session
        return session

    def sdk_get_browser_session(self, request: Any) -> FakeSessionContainer | None:
        """Fake for ``accounts_web._sdk_get_browser_session``: resolves the test cookie."""
        self._raise_if_configured("sdk_get_browser_session")
        token = request.cookies.get(self.BROWSER_SESSION_COOKIE, "")
        return self.sessions_by_access_token.get(token)

    def verify_turnstile_token(self, token: str, remote_ip: str | None) -> bool:
        """Fake for ``accounts_web._verify_turnstile_token``: driven by ``is_turnstile_passing``."""
        del token, remote_ip
        return self.is_turnstile_passing

    def client_ip(self, request: Any) -> str | None:
        """Fake for ``accounts_web._client_ip``: driven by ``fake_client_ip``."""
        del request
        return self.fake_client_ip

    def is_user_suspended_check(self, user_id: str) -> bool:
        """Fake for ``suspension.is_user_suspended``: driven by ``suspended_user_ids``.

        Patched on the suspension module, so ``is_user_suspended_at_gate`` and
        ``require_not_suspended`` (which resolve the check through the module
        global) honor it too.
        """
        return user_id in self.suspended_user_ids

    def sdk_delete_user(self, user_id: str) -> None:
        """Fake for the SDK's ``delete_user`` (the refused-OAuth-signup rollback)."""
        self._raise_if_configured("delete_user")
        account = self.accounts_by_id.pop(user_id, None)
        if account is None:
            return
        emails_pointing_at_account = [
            email for email, existing in self.accounts_by_email.items() if existing.user_id == user_id
        ]
        for email in emails_pointing_at_account:
            del self.accounts_by_email[email]
        self.revoke_all_sessions_for_user(user_id=user_id)

    def manually_create_or_update_user(
        self,
        *,
        tenant_id: str,
        third_party_id: str,
        third_party_user_id: str,
        email: str,
        is_verified: bool,
        user_context: dict[str, Any] | None = None,
    ) -> ManuallyCreateOrUpdateUserOkResult:
        del tenant_id, user_context
        # The real SDK call is keyed by third-party identity, not email: match on
        # (third_party_id, third_party_user_id) so a returning OAuth sign-in resolves
        # to the OAuth account even when a same-email password account also exists.
        existing = next(
            (
                candidate
                for candidate in self.accounts_by_id.values()
                if candidate.provider_id == third_party_id and candidate.third_party_user_id == third_party_user_id
            ),
            None,
        )
        created_new = existing is None
        if existing is None:
            account = _make_account(
                email=email,
                password=None,
                provider_id=third_party_id,
                third_party_user_id=third_party_user_id,
                display_name=None,
                is_verified=is_verified,
            )
            # setdefault: never clobber a same-email password account, which
            # ``sign_in`` looks up through this dict.
            self.accounts_by_email.setdefault(email, account)
            self.accounts_by_id[account.user_id] = account
        else:
            account = existing
            account.is_verified = account.is_verified or is_verified
        user = _build_st_user(account)
        return ManuallyCreateOrUpdateUserOkResult(
            user=user,
            recipe_user_id=RecipeUserId(account.user_id),
            created_new_recipe_user=created_new,
        )


def make_fake_supertokens_backend() -> FakeSuperTokensBackend:
    """Construct an empty in-memory SuperTokens backend."""
    backend = FakeSuperTokensBackend()
    backend.accounts_by_id = {}
    backend.accounts_by_email = {}
    backend.sessions_by_access_token = {}
    backend.sessions_by_refresh_token = {}
    backend.reset_tokens = {}
    backend.verification_tokens = {}
    backend.registered_providers = {}
    backend.sent_verification_emails = []
    backend.sent_reset_emails = []
    backend.sdk_errors_by_method = {}
    backend.last_browser_session = None
    backend.is_turnstile_passing = True
    backend.device_code_store = InMemoryDeviceAuthCodeStore()
    backend.attribution_store = InMemoryAttributionStore()
    backend.entitlements_store = make_fake_entitlements_store()
    backend.signup_attempt_store = InMemorySignupAttemptStore()
    backend.ip_reputation_cache = InMemoryIpReputationCache()
    backend.ip_reputation_provider = FakeIpReputationProvider()
    backend.tor_exit_list = FakeTorExitList()
    backend.fake_client_ip = "203.0.113.77"
    backend.suspended_user_ids = set()
    return backend


# ---------------------------------------------------------------------------
# Host pool fakes
#
# Similar to FakeSuperTokensBackend, this provides an in-memory replacement
# for the psycopg2 database and paramiko SSH operations used by the host pool
# endpoints.  ``FakePoolBackend.install_on_app_module`` patches the module
# references through a single for-loop (same pattern as the SuperTokens fakes)
# so the test-patching ratchet count increases by exactly one line.
# ---------------------------------------------------------------------------


# Placeholder host public keys for fake pool rows. The fake replaces the real
# SSH layer (``_append_authorized_key``), so these are never parsed/pinned -- they
# only need to be non-null so the lease fail-closed check passes.
_FAKE_OUTER_HOST_PUBLIC_KEY: Final[str] = "ssh-ed25519 AAAAFAKEouterhostkey"
_FAKE_CONTAINER_HOST_PUBLIC_KEY: Final[str] = "ssh-ed25519 AAAAFAKEcontainerhostkey"


class FakePoolRow:
    """In-memory record for a single pool_hosts row."""

    host_id: UUID
    # Placement columns are None while the workspace is stopped (mirrors the
    # nullable DB columns from migration 024).
    vps_address: str | None
    vps_instance_id: str
    agent_id: str
    host_id_str: str
    host_name: str
    ssh_port: int | None
    ssh_user: str
    container_ssh_port: int | None
    status: str
    version: str
    attributes: dict[str, Any] | None
    region: str | None
    leased_to_user: str | None
    leased_at: str | None
    released_at: str | None
    lima_instance_name: str | None
    lima_disk_name: str | None
    bare_metal_server_id: UUID | None
    outer_host_public_key: str | None
    container_host_public_key: str | None
    stop_requested_at: datetime | None
    stopped_at: datetime | None
    artifact_manifest: dict[str, Any] | None
    wrapped_dek: str | None
    artifact_generation: int
    transition_heartbeat_at: datetime | None
    transition_error: str | None
    transition_id: str | None
    transition_failure_count: int


def _row_attributes(row: "FakePoolRow") -> dict[str, Any]:
    """Return the JSONB attributes view of a fake row.

    Existing tests pass ``version="v…"`` for ergonomics; we synthesise a
    matching attributes dict from that here so the fake's behaviour mirrors
    what production does once the operator pool bake (``minds-admin pool
    create``) writes attributes directly.
    """
    if isinstance(row.attributes, dict):
        return dict(row.attributes)
    return {"version": row.version}


def _attributes_contain(row_attrs: dict[str, Any], requested: dict[str, Any]) -> bool:
    """Reproduce PostgreSQL's ``@>`` containment for primitive-valued attribute dicts."""
    for key, value in requested.items():
        if key not in row_attrs:
            return False
        if row_attrs[key] != value:
            return False
    return True


def _make_pool_row(
    host_id: UUID,
    vps_address: str,
    agent_id: str,
    host_id_str: str,
    ssh_port: int,
    ssh_user: str,
    container_ssh_port: int,
    version: str,
    status: str = "available",
    leased_to_user: str | None = None,
    leased_at: str | None = None,
    host_name: str | None = None,
    region: str | None = None,
    outer_host_public_key: str | None = _FAKE_OUTER_HOST_PUBLIC_KEY,
    container_host_public_key: str | None = _FAKE_CONTAINER_HOST_PUBLIC_KEY,
) -> FakePoolRow:
    row = FakePoolRow()
    row.host_id = host_id
    row.vps_address = vps_address
    row.vps_instance_id = f"vps-{host_id}"
    row.agent_id = agent_id
    row.host_id_str = host_id_str
    # Matches the migration's backfill: pre-leased rows default to host_id_str
    # so they remain visible under a stable name until something leases them.
    row.host_name = host_name if host_name is not None else host_id_str
    row.ssh_port = ssh_port
    row.ssh_user = ssh_user
    row.container_ssh_port = container_ssh_port
    row.status = status
    row.version = version
    row.leased_to_user = leased_to_user
    row.leased_at = leased_at
    row.released_at = None
    row.attributes = None
    row.region = region
    # Slice-specific tests set these explicitly.
    row.lima_instance_name = None
    row.lima_disk_name = None
    row.bare_metal_server_id = None
    row.outer_host_public_key = outer_host_public_key
    row.container_host_public_key = container_host_public_key
    row.stop_requested_at = None
    row.stopped_at = None
    row.artifact_manifest = None
    row.wrapped_dek = None
    row.artifact_generation = 0
    row.transition_heartbeat_at = None
    row.transition_error = None
    row.transition_id = None
    row.transition_failure_count = 0
    return row


def _statuses_in_query(query_lower: str) -> set[str]:
    """The single-quoted status literals a query's IN (...) list names."""
    return set(re.findall(r"'([a-z_]+)'", query_lower))


# Fixed timestamps for fake workspace-sync rows; list order stands in for
# ORDER BY created_at (rows are only ever appended).
_SYNC_ROW_CREATED_AT: Final[str] = "2026-01-01T00:00:00+00:00"
_SYNC_ROW_UPDATED_AT: Final[str] = "2026-01-02T00:00:00+00:00"

# Derived from the production column list so the fake's tuple order can never
# drift from what PostgresSyncStore SELECTs.
_WORKSPACE_RECORD_COLUMN_NAMES: Final[tuple[str, ...]] = tuple(
    name.strip() for name in _WORKSPACE_RECORD_COLUMNS.split(",")
)


def _adapted_bytes(value: Any) -> bytes | None:
    """Unwrap a psycopg2.Binary bind parameter back to the raw bytes (None passes through)."""
    if value is None:
        return None
    return bytes(value.adapted)


class FakeCursor:
    """In-memory cursor that simulates psycopg2 cursor behavior against FakePoolBackend."""

    _backend: "FakePoolBackend"
    _results: list[tuple[Any, ...]]
    rowcount: int

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        """Route SQL queries to the in-memory store."""
        self._results = []
        self._result_idx = 0
        self.rowcount = 0
        query_lower = query.strip().lower()

        if "pg_advisory_xact_lock" in query_lower:
            # The per-user lease serialization lock; the in-memory fake is
            # single-threaded so the lock itself is a no-op.
            self._results = [(True,)]

        elif "select count(*) from pool_hosts" in query_lower:
            user_id_prefix = params[0]
            counted_statuses = _statuses_in_query(query_lower) or {"leased"}
            count = sum(
                1
                for row in self._backend.pool_rows
                if row.status in counted_statuses and row.leased_to_user == user_id_prefix
            )
            self._results = [(count,)]

        elif query_lower.startswith("select id, status, leased_to_user, host_id"):
            # stop_start supervisor: full-row read by id.
            found = self._backend.find_pool_row(params[0])
            if found is not None:
                self._results = [self._backend.workspace_supervisor_tuple(found)]

        elif query_lower.startswith("select status from pool_hosts"):
            found = self._backend.find_pool_row(params[0])
            if found is not None:
                self._results = [(found.status,)]

        elif query_lower.startswith("select leased_to_user, id, status"):
            # workspaces.py: owned-workspace read (ownership column + info columns).
            found = self._backend.find_pool_row(params[0])
            if found is not None:
                self._results = [(found.leased_to_user,) + self._backend.workspace_info_tuple(found)]

        elif "from pool_hosts" in query_lower and "order by leased_at" in query_lower:
            # workspaces.py: full-lifecycle list endpoint.
            listed_statuses = _statuses_in_query(query_lower)
            for row in self._backend.pool_rows:
                if row.leased_to_user == params[0] and row.status in listed_statuses:
                    self._results.append(self._backend.workspace_info_tuple(row))

        elif query_lower.startswith("update pool_hosts set status = 'stopping', stop_requested_at"):
            transition_id, raw_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.status == "leased":
                found.status = "stopping"
                found.stop_requested_at = datetime.now(timezone.utc)
                found.transition_error = None
                found.transition_failure_count = 0
                found.transition_id = transition_id
                found.transition_heartbeat_at = datetime.now(timezone.utc)
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set status = 'starting'"):
            transition_id, raw_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.status == "stopped":
                found.status = "starting"
                found.transition_error = None
                found.transition_failure_count = 0
                found.transition_id = transition_id
                found.transition_heartbeat_at = datetime.now(timezone.utc)
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set status = 'crashed'"):
            reason, transition_id, raw_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.status in ("leased", "stopping", "stopped", "starting"):
                found.status = "crashed"
                found.transition_error = reason
                found.transition_id = transition_id
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set transition_heartbeat_at"):
            raw_id, transition_id, expected_status = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.transition_id == transition_id and found.status == expected_status:
                found.transition_heartbeat_at = datetime.now(timezone.utc)
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set transition_error"):
            message, raw_id, transition_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.transition_id == transition_id:
                found.transition_error = message
                found.transition_failure_count = found.transition_failure_count + 1
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set wrapped_dek"):
            wrapped, manifest_json, raw_id, transition_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.status == "stopping" and found.transition_id == transition_id:
                found.wrapped_dek = wrapped
                found.artifact_manifest = json.loads(manifest_json)
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set status = 'stopped', stopped_at"):
            manifest_json, generation, raw_id, transition_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.status == "stopping" and found.transition_id == transition_id:
                found.status = "stopped"
                found.stopped_at = datetime.now(timezone.utc)
                found.artifact_manifest = json.loads(manifest_json)
                found.artifact_generation = int(generation)
                found.transition_error = None
                found.transition_failure_count = 0
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set status = 'stopped', transition_error"):
            message, raw_id, transition_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.status == "starting" and found.transition_id == transition_id:
                found.status = "stopped"
                found.transition_error = message
                found.transition_failure_count = found.transition_failure_count + 1
                found.transition_heartbeat_at = None
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set status = 'leased', stop_requested_at = null"):
            generation, raw_id, transition_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.status == "starting" and found.transition_id == transition_id:
                found.status = "leased"
                found.stop_requested_at = None
                found.stopped_at = None
                found.artifact_manifest = None
                found.wrapped_dek = None
                found.artifact_generation = int(generation)
                found.transition_error = None
                found.transition_failure_count = 0
                found.transition_heartbeat_at = None
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set status = 'leased', vps_address"):
            vps_address, ssh_port, container_ssh_port, server_id, raw_id, transition_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.status == "starting" and found.transition_id == transition_id:
                found.status = "leased"
                found.vps_address = vps_address
                found.ssh_port = ssh_port
                found.container_ssh_port = container_ssh_port
                found.bare_metal_server_id = UUID(server_id) if isinstance(server_id, str) else server_id
                found.stop_requested_at = None
                found.stopped_at = None
                found.transition_error = None
                found.transition_failure_count = 0
                found.transition_heartbeat_at = None
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set vps_address = null"):
            # Retention finalize, step 1: claim the VM for deletion by
            # clearing the placement (guarded on ownership).
            raw_id, transition_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.status == "stopped" and found.transition_id == transition_id:
                found.vps_address = None
                found.ssh_port = None
                found.container_ssh_port = None
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set bare_metal_server_id = null"):
            # Retention finalize, final step: drop the box link after the VM
            # is gone (guarded on ownership).
            raw_id, transition_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None and found.status == "stopped" and found.transition_id == transition_id:
                found.bare_metal_server_id = None
                found.transition_heartbeat_at = None
                self.rowcount = 1

        elif query_lower.startswith("update pool_hosts set transition_id"):
            # Watchdog takeover: claim only an in-flight (or unfinalized-stop)
            # row whose heartbeat is still stale.
            transition_id, raw_id = params
            found = self._backend.find_pool_row(raw_id)
            if found is not None:
                is_in_flight = found.status in ("stopping", "starting") or (
                    found.status == "stopped" and found.bare_metal_server_id is not None
                )
                heartbeat = found.transition_heartbeat_at
                age = None if heartbeat is None else (datetime.now(timezone.utc) - heartbeat).total_seconds()
                if is_in_flight and (age is None or age >= stop_start_module.STALE_HEARTBEAT_SECONDS):
                    found.transition_id = transition_id
                    found.transition_heartbeat_at = datetime.now(timezone.utc)
                    self.rowcount = 1

        elif query_lower.startswith("select id, status, transition_failure_count"):
            # Watchdog: every in-flight (or unfinalized-stop) row, with its
            # failure count and heartbeat age (None when never stamped).
            for row in self._backend.pool_rows:
                is_in_flight = row.status in ("stopping", "starting") or (
                    row.status == "stopped" and row.bare_metal_server_id is not None
                )
                if not is_in_flight:
                    continue
                heartbeat = row.transition_heartbeat_at
                age = None if heartbeat is None else (datetime.now(timezone.utc) - heartbeat).total_seconds()
                self._results.append((row.host_id, row.status, row.transition_failure_count, age))

        elif "from bare_metal_servers where id" in query_lower:
            box = self._backend.find_box_row(params[0])
            if box is not None:
                self._results = [self._backend.box_tuple(box)]

        elif "from bare_metal_servers where status = 'ready'" in query_lower:
            # The candidate-box listing selects the box columns plus region.
            self._results = [
                self._backend.box_tuple(box) + (box["region"],)
                for box in self._backend.box_rows
                if box["status"] == "ready"
            ]

        elif "from pool_hosts" in query_lower and "status = 'available'" in query_lower:
            # The connector serialises the request attributes via json.dumps
            # before passing them to the SQL bind parameter, so we always get
            # a JSON string here. A hard ``region`` (WHERE clause), if present,
            # follows it in the param tuple.
            raw = params[0]
            requested = json.loads(raw) if isinstance(raw, str) else dict(raw)
            # A hard ``region`` bind param, when present, always immediately
            # follows the attributes JSON param (index 0), so its index is 1.
            hard_region: str | None = None
            if "and region = %s" in query_lower:
                hard_region = params[1]
            candidate_rows = [
                row
                for row in self._backend.pool_rows
                if row.status == "available"
                and _attributes_contain(_row_attributes(row), requested)
                and (hard_region is None or row.region == hard_region)
            ]
            if candidate_rows:
                chosen = candidate_rows[0]
                self._results = [
                    (
                        chosen.host_id,
                        chosen.vps_address,
                        chosen.ssh_port,
                        chosen.ssh_user,
                        chosen.container_ssh_port,
                        chosen.agent_id,
                        chosen.host_id_str,
                        _row_attributes(chosen),
                        chosen.outer_host_public_key,
                        chosen.container_host_public_key,
                    )
                ]

        elif "update pool_hosts set status = 'leased'" in query_lower:
            # Lease SQL now also writes the user-supplied host_name on the
            # same UPDATE so the friendly name is set atomically with the
            # status flip.
            user_id_prefix, host_name, host_id = params
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    row.status = "leased"
                    row.leased_to_user = user_id_prefix
                    row.leased_at = "2026-01-01T00:00:00+00:00"
                    row.host_name = host_name
                    break

        elif query_lower.startswith("select leased_to_user, status, vps_address"):
            # Enable-sharing endpoint: lookup by id returning the columns the
            # server-side share injection needs (ownership, status, the
            # container SSH coordinates, host id, and the pinned container key).
            raw_host_id = params[0]
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    self._results = [
                        (
                            row.leased_to_user,
                            row.status,
                            row.vps_address,
                            row.container_ssh_port,
                            row.ssh_user,
                            row.host_id_str,
                            row.container_host_public_key,
                            row.agent_id,
                        )
                    ]
                    break

        elif "select leased_to_user, status from pool_hosts" in query_lower:
            # Rename endpoint: a narrow ownership/status lookup by id (only
            # ``leased_to_user`` and ``status``). Matched before the broader
            # release lookup below, which selects additional columns.
            raw_host_id = params[0]
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    self._results = [(row.leased_to_user, row.status)]
                    break

        elif "update pool_hosts set host_name" in query_lower:
            # Rename endpoint: set the mutable friendly name by id.
            host_name, raw_host_id = params
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    row.host_name = host_name
                    break

        elif (
            "from pool_hosts" in query_lower
            and "leased_to_user" in query_lower
            and "select leased_to_user" in query_lower
        ):
            # The release projection (``hosts._read_pool_row_for_release``
            # reads it, in this column order). The connector stringifies the
            # UUID before passing it as a bind param (psycopg2 can't adapt
            # Python ``UUID`` directly), so accept either form.
            raw_host_id = params[0]
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    self._results = [
                        (
                            row.leased_to_user,
                            row.status,
                            row.lima_instance_name,
                            row.lima_disk_name,
                            row.bare_metal_server_id,
                            row.host_id_str,
                            row.agent_id,
                        )
                    ]
                    break

        elif (
            "from pool_hosts" in query_lower and "status = 'leased'" in query_lower and "leased_to_user" in query_lower
        ):
            # List endpoint: lookup by user
            user_id_prefix = params[0]
            for row in self._backend.pool_rows:
                if row.status == "leased" and row.leased_to_user == user_id_prefix:
                    self._results.append(
                        (
                            row.host_id,
                            row.vps_address,
                            row.ssh_port,
                            row.ssh_user,
                            row.container_ssh_port,
                            row.agent_id,
                            row.host_id_str,
                            row.host_name,
                            _row_attributes(row),
                            row.leased_at,
                            row.outer_host_public_key,
                            row.container_host_public_key,
                        )
                    )

        elif "update pool_hosts set status = 'unreachable'" in query_lower:
            # Lease-time quarantine of a row whose SSH key injection failed.
            raw_host_id = params[0]
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    row.status = "unreachable"
                    break

        elif "update pool_hosts set status = 'removing'" in query_lower:
            raw_host_id = params[0]
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            for row in self._backend.pool_rows:
                if row.host_id == host_id:
                    row.status = "removing"
                    row.released_at = "2026-01-02T00:00:00+00:00"
                    # The crashed-release flip also clears the box link, so a
                    # retry never re-requires the dead box's teardown.
                    if "bare_metal_server_id = null" in query_lower:
                        row.bare_metal_server_id = None
                    break

        elif "from paid_emails" in query_lower and "select 1" in query_lower:
            entry = self._backend.paid_emails.get(params[0])
            if entry is not None and entry["is_paid"]:
                self._results = [(1,)]

        elif "from paid_domains" in query_lower and "select 1" in query_lower:
            entry = self._backend.paid_domains.get(params[0])
            if entry is not None and entry["is_paid"]:
                self._results = [(1,)]

        elif "from paid_emails" in query_lower and "select email" in query_lower:
            self._results = self._backend.list_paid_entries(
                self._backend.paid_emails, paid_only="is_paid = true" in query_lower
            )

        elif "from paid_domains" in query_lower and "select domain" in query_lower:
            self._results = self._backend.list_paid_entries(
                self._backend.paid_domains, paid_only="is_paid = true" in query_lower
            )

        elif "insert into paid_emails" in query_lower:
            self._backend.activate_paid_entry(self._backend.paid_emails, params[0])

        elif "insert into paid_domains" in query_lower:
            self._backend.activate_paid_entry(self._backend.paid_domains, params[0])

        elif "update paid_emails set is_paid = false" in query_lower:
            self._backend.deactivate_paid_entry(self._backend.paid_emails, params[0])

        elif "update paid_domains set is_paid = false" in query_lower:
            self._backend.deactivate_paid_entry(self._backend.paid_domains, params[0])

        elif query_lower.startswith("delete from pool_hosts where id"):
            raw_host_id = params[0]
            host_id = UUID(raw_host_id) if isinstance(raw_host_id, str) else raw_host_id
            self._backend.pool_rows = [r for r in self._backend.pool_rows if r.host_id != host_id]

        elif "from workspace_records" in query_lower and "for update" in query_lower:
            record_row = self._backend.find_sync_record_by_agent(params[0], params[1])
            if record_row is not None:
                self._results = [self._backend.sync_record_tuple(record_row)]

        elif "from workspace_records" in query_lower and "order by created_at" in query_lower:
            self._results = [
                self._backend.sync_record_tuple(row)
                for row in self._backend.sync_record_rows
                if row["user_id"] == params[0]
            ]

        elif query_lower.startswith("insert into workspace_records") and "do nothing" in query_lower:
            # The lease-time record stub: no-op when the workspace already has
            # a record (ON CONFLICT (user_id, agent_id) DO NOTHING).
            self._backend.insert_lease_record_stub(params)

        elif query_lower.startswith("insert into workspace_records"):
            self._results = [self._backend.sync_record_tuple(self._backend.insert_sync_record(params))]

        elif query_lower.startswith("update workspace_records set state = 'destroyed'"):
            # The release-time tombstone, addressed by (agent_id, user-id prefix).
            agent_id, user_id_prefix = params
            for row in self._backend.sync_record_rows:
                if (
                    row["agent_id"] == agent_id
                    and derive_user_id_prefix(row["user_id"]) == user_id_prefix
                    and row["state"] == "active"
                ):
                    row["state"] = "destroyed"
                    row["destroyed_at"] = row.get("destroyed_at") or datetime.now(timezone.utc)
                    row["revision"] = int(row["revision"]) + 1
                    row["updated_at"] = _SYNC_ROW_UPDATED_AT
                    self.rowcount += 1

        elif query_lower.startswith("select 1 from pool_hosts"):
            # The tombstone-first guard: does the user hold a lease for the
            # workspace (by agent id) or its current host (by host id)?
            key, user_id_prefix = params
            held_statuses = _statuses_in_query(query_lower)
            for row in self._backend.pool_rows:
                row_key = row.agent_id if "where agent_id" in query_lower else row.host_id_str
                if row_key == key and row.leased_to_user == user_id_prefix and row.status in held_statuses:
                    self._results = [(1,)]
                    break

        elif "from pool_hosts p left join workspace_records r" in query_lower:
            # The lease-vs-record sweep's join: every lease-holding row paired
            # with the owner's record for the same workspace (or NULLs).
            held_statuses = _statuses_in_query(query_lower)
            for row in self._backend.pool_rows:
                if row.status not in held_statuses:
                    continue
                record = next(
                    (
                        candidate
                        for candidate in self._backend.sync_record_rows
                        if candidate["agent_id"] == row.agent_id
                        and derive_user_id_prefix(candidate["user_id"]) == row.leased_to_user
                    ),
                    None,
                )
                self._results.append(
                    (
                        row.host_id,
                        row.status,
                        row.agent_id,
                        row.host_id_str,
                        row.leased_to_user,
                        row.released_at,
                        record["state"] if record is not None else None,
                        record.get("destroyed_at") if record is not None else None,
                    )
                )

        elif query_lower.startswith("update workspace_records set encrypted_secrets = null"):
            self.rowcount = self._backend.scrub_sync_secrets(params[0])

        elif query_lower.startswith("update workspace_records"):
            updated_row = self._backend.update_sync_record(query, params)
            if updated_row is not None:
                self._results = [self._backend.sync_record_tuple(updated_row)]

        elif query_lower.startswith("delete from workspace_records") and "revision = 1" in query_lower:
            # The release-time retirement of a never-written lease stub.
            agent_id, user_id_prefix = params
            kept_rows = []
            for row in self._backend.sync_record_rows:
                is_untouched_stub = (
                    row["agent_id"] == agent_id
                    and derive_user_id_prefix(row["user_id"]) == user_id_prefix
                    and row["state"] == "active"
                    and int(row["revision"]) == 1
                    and row.get("encrypted_secrets") is None
                    and row.get("backup_bucket") is None
                )
                if is_untouched_stub:
                    self.rowcount += 1
                else:
                    kept_rows.append(row)
            self._backend.sync_record_rows = kept_rows

        elif query_lower.startswith("delete from workspace_records"):
            user_id, record_key = params
            key_column = "agent_id" if "agent_id = %s" in query_lower else "host_id"
            self._backend.sync_record_rows = [
                row
                for row in self._backend.sync_record_rows
                if not (row["user_id"] == user_id and row[key_column] == record_key)
            ]

        elif query_lower.startswith("select 1 from workspace_records") and "substring" in query_lower:
            # With the trailing "AND agent_id <> %s" exclusion the query
            # carries a fifth parameter (the excluded workspace id).
            if "agent_id <> %s" in query_lower:
                bucket_name, record_short_a, record_short_b, user_id_prefix, excluded_workspace_id = params
            else:
                bucket_name, record_short_a, record_short_b, user_id_prefix = params
                excluded_workspace_id = None
            for row in self._backend.sync_record_rows:
                is_referenced = (
                    row.get("backup_bucket") == bucket_name
                    or row["host_id"] == record_short_a
                    or row["agent_id"] == record_short_b
                )
                if excluded_workspace_id is not None and row["agent_id"] == excluded_workspace_id:
                    continue
                if is_referenced and derive_user_id_prefix(row["user_id"]) == user_id_prefix:
                    self._results = [(1,)]
                    break

        elif query_lower.startswith("select 1 from workspace_records") and "state = 'active'" in query_lower:
            active_row = self._backend.find_sync_record_by_short_name(params[0], params[1])
            if active_row is not None and active_row["state"] == "active":
                self._results = [(1,)]

        elif query_lower.startswith("select 1 from workspace_records"):
            if self._backend.find_sync_record_by_short_name(params[0], params[1]) is not None:
                self._results = [(1,)]

        elif query_lower.startswith(
            "select r.user_id, r.host_id, r.agent_id, r.backup_bucket, r.destroyed_at from workspace_records r"
        ):
            # Aged tombstones, minus those whose workspace still holds a lease
            # (the NOT EXISTS against pool_hosts).
            cutoff = params[0]
            held_statuses = _statuses_in_query(query_lower) - {"destroyed"}
            reapable = [
                (row["user_id"], row["host_id"], row["agent_id"], row.get("backup_bucket"), row["destroyed_at"])
                for row in self._backend.sync_record_rows
                if row["state"] == "destroyed"
                and row.get("destroyed_at") is not None
                and row["destroyed_at"] < cutoff
                and not any(
                    pool_row.agent_id == row["agent_id"]
                    and pool_row.leased_to_user == derive_user_id_prefix(row["user_id"])
                    and pool_row.status in held_statuses
                    for pool_row in self._backend.pool_rows
                )
            ]
            self._results = sorted(reapable, key=lambda reap_row: reap_row[4])

        elif query_lower.startswith("insert into orphan_backup_buckets"):
            stamp = self._backend.orphan_stamps.setdefault(params[0], datetime.now(timezone.utc))
            self._results = [(stamp,)]

        elif query_lower.startswith("select first_seen_orphaned_at from orphan_backup_buckets"):
            existing_stamp = self._backend.orphan_stamps.get(params[0])
            if existing_stamp is not None:
                self._results = [(existing_stamp,)]

        elif query_lower.startswith("delete from orphan_backup_buckets"):
            self._backend.orphan_stamps.pop(params[0], None)

        elif query_lower.startswith("select") and "from account_key_bundles" in query_lower:
            bundle = self._backend.sync_bundle_by_user.get(params[0])
            if bundle is not None:
                self._results = [
                    (
                        bundle["kdf_salt"],
                        bundle["kdf_time_cost"],
                        bundle["kdf_memory_kib"],
                        bundle["kdf_parallelism"],
                        bundle["wrapped_dek"],
                        bundle["key_epoch"],
                        _SYNC_ROW_UPDATED_AT,
                    )
                ]

        elif query_lower.startswith("insert into account_key_bundles"):
            user_id, kdf_salt, kdf_time_cost, kdf_memory_kib, kdf_parallelism, wrapped_dek, key_epoch = params
            if "do nothing" in query_lower and user_id in self._backend.sync_bundle_by_user:
                # The create-only insert: an existing row wins and the
                # statement affects nothing, mirroring ON CONFLICT DO NOTHING.
                self.rowcount = 0
            else:
                self.rowcount = 1
                self._backend.sync_bundle_by_user[user_id] = {
                    "kdf_salt": _adapted_bytes(kdf_salt),
                    "kdf_time_cost": kdf_time_cost,
                    "kdf_memory_kib": kdf_memory_kib,
                    "kdf_parallelism": kdf_parallelism,
                    "wrapped_dek": _adapted_bytes(wrapped_dek),
                    "key_epoch": key_epoch,
                }

        elif query_lower.startswith("delete from account_key_bundles"):
            self._backend.sync_bundle_by_user.pop(params[0], None)

        elif query_lower.startswith("select count(*) from shares"):
            user_label, exclude_host_id = params
            active_count = sum(
                1
                for share in self._backend.share_rows
                if share["user_id"] == user_label
                and share["state"] == "active"
                and share["host_id"] != exclude_host_id
            )
            self._results = [(active_count,)]

        elif query_lower.startswith("insert into shares"):
            host_id, user_label, region, workspace_domain, entry_label, workspace_id, share_label = params
            self._backend.upsert_share(
                host_id,
                user_label,
                region,
                workspace_domain,
                entry_label=entry_label,
                workspace_id=workspace_id,
                share_label=share_label,
            )

        elif "from shares where workspace_id = %s and user_id = %s" in query_lower:
            for share in self._backend.share_rows:
                if share.get("workspace_id") == params[0] and share["user_id"] == params[1]:
                    self._results = [self._backend.share_tuple(share)]
                    break

        elif "from shares where host_id = %s and user_id = %s" in query_lower:
            share = self._backend.find_share(params[0], params[1])
            if share is not None:
                self._results = [self._backend.share_tuple(share)]

        elif "from shares where workspace_domain = %s and state = 'active'" in query_lower:
            for share in self._backend.share_rows:
                if share["workspace_domain"] == params[0] and share["state"] == "active":
                    self._results = [self._backend.share_tuple(share)]
                    break

        elif "from shares where user_id = %s" in query_lower:
            self._results = [
                self._backend.share_tuple(share) for share in self._backend.share_rows if share["user_id"] == params[0]
            ]

        elif query_lower.startswith("update shares set state = 'inactive'"):
            deactivated_share = self._backend.find_share(params[0], params[1])
            if deactivated_share is not None:
                deactivated_share["state"] = "inactive"
                deactivated_share["updated_at"] = _SHARE_ROW_UPDATED_AT

        elif query_lower.startswith("update shares set state = 'suspended'"):
            # Suspension: every active share of the user flips to suspended.
            for share in self._backend.share_rows:
                if share["user_id"] == params[0] and share["state"] == "active":
                    share["state"] = "suspended"
                    share["updated_at"] = _SHARE_ROW_UPDATED_AT
                    self.rowcount += 1

        elif query_lower.startswith("update shares set state = 'active'") and "state = 'suspended'" in query_lower:
            for share in self._backend.share_rows:
                if share["user_id"] == params[0] and share["state"] == "suspended":
                    share["state"] = "active"
                    share["updated_at"] = _SHARE_ROW_UPDATED_AT
                    self.rowcount += 1

        elif query_lower.startswith("update shares set entry_label"):
            entry_label, host_id, user_label = params
            labeled_share = self._backend.find_share(host_id, user_label)
            if labeled_share is not None:
                labeled_share["entry_label"] = entry_label
                labeled_share["updated_at"] = _SHARE_ROW_UPDATED_AT

        elif query_lower.startswith("update shares set last_tunnel_login_at"):
            logged_in_share = self._backend.find_share(params[0], params[1])
            if logged_in_share is not None:
                logged_in_share["last_tunnel_login_at"] = _SHARE_ROW_UPDATED_AT
                logged_in_share["updated_at"] = _SHARE_ROW_UPDATED_AT

        elif query_lower.startswith("delete from relay_tokens"):
            host_id, user_label = params
            self._backend.relay_token_rows = [
                token_row
                for token_row in self._backend.relay_token_rows
                if not (token_row["host_id"] == host_id and token_row["user_id"] == user_label)
            ]

        elif query_lower.startswith("insert into relay_tokens"):
            token_hash, host_id, user_label = params
            self._backend.relay_token_rows.append(
                {"token_hash": token_hash, "host_id": host_id, "user_id": user_label}
            )

        elif "from relay_tokens rt join shares s" in query_lower:
            for token_row in self._backend.relay_token_rows:
                if token_row["token_hash"] == params[0]:
                    joined_share = self._backend.find_share(token_row["host_id"], token_row["user_id"])
                    if joined_share is not None:
                        self._results = [
                            (
                                joined_share["host_id"],
                                joined_share["user_id"],
                                joined_share["region"],
                                joined_share["workspace_domain"],
                                joined_share["state"],
                            )
                        ]
                    break

        elif query_lower.startswith("select region from pool_hosts"):
            for pool_row in self._backend.pool_rows:
                if pool_row.host_id_str == params[0]:
                    self._results = [(pool_row.region,)]
                    break

        elif "from relays order by relay_id" in query_lower:
            self._results = [
                self._backend.relay_tuple(relay_row)
                for relay_row in sorted(self._backend.relay_rows, key=lambda row: row["relay_id"])
            ]

        elif query_lower.startswith("insert into relays"):
            relay_id, region, tunnel_endpoint, ip_address, instance_name = params
            self._backend.upsert_relay(relay_id, region, tunnel_endpoint, ip_address, instance_name)

        elif query_lower.startswith("update relays set is_active = false"):
            self.rowcount = 0
            for relay_row in self._backend.relay_rows:
                if relay_row["relay_id"] == params[0]:
                    relay_row["is_active"] = False
                    self.rowcount = 1

        elif query_lower.startswith("update relays set health"):
            health, consecutive_probe_failures, relay_id = params
            for relay_row in self._backend.relay_rows:
                if relay_row["relay_id"] == relay_id:
                    relay_row["health"] = health
                    relay_row["consecutive_probe_failures"] = consecutive_probe_failures

        elif query_lower.startswith("insert into share_tunnel_logins"):
            host_id, user_label, relay_id = params
            for login_row in self._backend.share_tunnel_login_rows:
                if (
                    login_row["host_id"] == host_id
                    and login_row["user_id"] == user_label
                    and login_row["relay_id"] == relay_id
                ):
                    login_row["last_login_at"] = _SHARE_ROW_UPDATED_AT
                    break
            else:
                self._backend.share_tunnel_login_rows.append(
                    {
                        "host_id": host_id,
                        "user_id": user_label,
                        "relay_id": relay_id,
                        "last_login_at": _SHARE_ROW_UPDATED_AT,
                    }
                )

        elif query_lower.startswith("select relay_id, last_login_at from share_tunnel_logins"):
            host_id, user_label = params
            self._results = [
                (login_row["relay_id"], login_row["last_login_at"])
                for login_row in sorted(self._backend.share_tunnel_login_rows, key=lambda row: row["relay_id"])
                if login_row["host_id"] == host_id and login_row["user_id"] == user_label
            ]

        elif query_lower.startswith("insert into issued_certs"):
            workspace_domain, host_id, user_label, ca_name, cert_chain_pem, sans_json, not_after = params
            self._backend.issued_cert_rows.append(
                {
                    "workspace_domain": workspace_domain,
                    "host_id": host_id,
                    "user_id": user_label,
                    "ca_name": ca_name,
                    "cert_chain_pem": cert_chain_pem,
                    "sans": sans_json,
                    "not_after": not_after,
                }
            )

        elif query_lower.startswith("select account_key_pem, account_uri from acme_accounts"):
            for account_row in self._backend.acme_account_rows:
                if account_row["ca_name"] == params[0] and account_row["directory_url"] == params[1]:
                    self._results = [(account_row["account_key_pem"], account_row["account_uri"])]
                    break

        elif query_lower.startswith("insert into acme_accounts"):
            ca_name, directory_url, account_key_pem, account_uri, eab_kid = params
            self._backend.acme_account_rows.append(
                {
                    "ca_name": ca_name,
                    "directory_url": directory_url,
                    "account_key_pem": account_key_pem,
                    "account_uri": account_uri,
                    "eab_kid": eab_kid,
                }
            )

        elif query_lower.startswith("select count(*) from issued_certs"):
            # The fake rows carry no created_at; every recorded issuance counts
            # as recent, which is what the rate-limit tests need.
            host_id, user_label = params
            count = sum(
                1
                for cert_row in self._backend.issued_cert_rows
                if cert_row["host_id"] == host_id and cert_row["user_id"] == user_label
            )
            self._results = [(count,)]

        elif query_lower.startswith("select not_after from issued_certs"):
            matching_not_afters = sorted(
                (
                    cert_row["not_after"]
                    for cert_row in self._backend.issued_cert_rows
                    if cert_row["workspace_domain"] == params[0]
                ),
                reverse=True,
            )
            if matching_not_afters:
                self._results = [(matching_not_afters[0],)]

        else:
            pass

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._results:
            return self._results[0]
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._results)

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _make_fake_cursor(backend: "FakePoolBackend") -> FakeCursor:
    cursor = FakeCursor()
    cursor._backend = backend
    cursor._results = []
    return cursor


class FakeConnection:
    """In-memory connection that simulates psycopg2 connection behavior."""

    _backend: "FakePoolBackend"

    def cursor(self) -> FakeCursor:
        return _make_fake_cursor(self._backend)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _make_fake_connection(backend: "FakePoolBackend") -> FakeConnection:
    conn = FakeConnection()
    conn._backend = backend
    return conn


_PAID_ENTRY_CREATED_AT = "2026-01-01T00:00:00+00:00"
_PAID_ENTRY_UPDATED_AT = "2026-01-02T00:00:00+00:00"

_SHARE_ROW_CREATED_AT = "2026-01-01T00:00:00+00:00"
_SHARE_ROW_UPDATED_AT = "2026-01-02T00:00:00+00:00"


def make_relay_row(
    relay_id: str,
    region: str = "us1",
    tunnel_endpoint: str | None = None,
    ip_address: str = "198.51.100.9",
    instance_name: str = "",
    is_active: bool = True,
    health: str = "healthy",
    consecutive_probe_failures: int = 0,
) -> dict[str, Any]:
    """One relays-table row dict, keyed like relays._RELAY_COLUMN_NAMES.

    The shape ``RelayStore.list_relays`` returns; the tunnel endpoint defaults
    to ``<ip>:7000`` (what the provisioning flow registers).
    """
    return {
        "relay_id": relay_id,
        "region": region,
        "tunnel_endpoint": tunnel_endpoint if tunnel_endpoint is not None else f"{ip_address}:7000",
        "ip_address": ip_address,
        "instance_name": instance_name,
        "is_active": is_active,
        "health": health,
        "consecutive_probe_failures": consecutive_probe_failures,
    }


def _split_sql_set_clauses(query: str) -> list[str]:
    """Split an UPDATE statement's SET clause on top-level commas (parenthesized commas kept intact)."""
    lowered = query.lower()
    set_start = lowered.index(" set ") + len(" set ")
    set_end = lowered.index(" where ")
    set_part = query[set_start:set_end]
    clauses: list[str] = []
    depth = 0
    current: list[str] = []
    for char in set_part:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        else:
            pass
        if char == "," and depth == 0:
            clauses.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    clauses.append("".join(current).strip())
    return clauses


class FakePoolBackend:
    """In-memory pool database replacement for testing host pool + paid-list endpoints."""

    pool_rows: list[FakePoolRow]
    append_key_calls: list[tuple[str, int, str, str, str, str]]
    # vps_address values whose SSH key injection fails (simulating a dead
    # host); the lease path quarantines such rows and tries the next one.
    append_key_failure_addresses: set[str]
    # Recorded server-side share-materials injections (SSH is faked): each entry
    # is ``(host, container_ssh_port, {remote_path: content})``.
    written_container_files: list[tuple[str, int, dict[str, str]]]
    # The seed-if-absent path set of each recorded injection, parallel to
    # ``written_container_files`` (paths whose write skips when the file exists).
    written_container_seed_only_paths: list[frozenset[str]]
    # Recorded web-claim adopts (SSH is faked): each entry is
    # ``(host, container_ssh_port, agent_id, host_name, display_name, connector_url)``.
    # Set ``adopt_should_fail`` to simulate an adopt that dies over SSH.
    adopted_containers: list[tuple[str, int, str, str, str, str]]
    adopt_should_fail: bool
    # Recorded web-claim agent starts (SSH is faked): each entry is
    # ``(host, container_ssh_port)``. Set ``agent_start_should_fail`` to
    # simulate a start script that exits non-zero.
    started_agent_containers: list[tuple[str, int]]
    agent_start_should_fail: bool
    # Recorded slice-VM teardowns (the box SSH is faked); set
    # ``slice_teardown_should_fail`` to simulate a teardown that cannot complete.
    slice_teardowns: list[tuple[Any, Any, str | None, str | None]]
    slice_teardown_should_fail: bool
    # Seeded bare_metal_servers rows for stop/start supervisor tests.
    box_rows: list[dict[str, Any]]
    # Workspace stop/start fakes: the tier storage config (None = unconfigured,
    # endpoints answer 503), recorded S3 prefix deletions (set
    # ``delete_prefix_should_fail`` to simulate a storage outage), the box command log,
    # files "written" to boxes, the transfer status feed (popped per read; the
    # last entry repeats), whether the detached transfer pid looks alive,
    # whether the VM still exists on its origin box (the restart-in-place
    # check), canned age-keygen/reserve outputs, spawned supervisor ids, an
    # optional substring that makes matching box commands fail, an optional
    # callback invoked with each box command before it is answered (for
    # mid-transfer state changes), and an optional callback invoked on each
    # supervisor sleep (for mid-wait state changes).
    storage_config: Any
    deleted_prefixes: list[str]
    delete_prefix_should_fail: bool
    box_command_log: list[str]
    box_file_writes: dict[str, str]
    transfer_status_sequence: list[str]
    transfer_alive: bool
    vm_exists_on_origin: bool
    age_keygen_output: str
    reserve_rc: int
    reserve_stdout: str
    reserve_stderr: str
    # When set, the restore rollback's ``limactl delete`` leaves the instance
    # config behind, so the box reports the VM survived.
    cleanup_vm_survives: bool
    spawned_supervisors: list[str]
    spawned_supervisor_tokens: list[tuple[str, str]]
    box_command_should_fail_matching: str | None
    box_command_callback: Any
    sleep_callback: Any
    # Paid-list stores: value -> {"is_paid", "created_at", "updated_at"}.
    paid_domains: dict[str, dict[str, Any]]
    paid_emails: dict[str, dict[str, Any]]
    # Workspace-sync stores: rows keyed by (user_id, host_id) held as dicts in
    # insertion order; bundles keyed by user_id. Secrets/salts are raw bytes.
    sync_record_rows: list[dict[str, Any]]
    sync_bundle_by_user: dict[str, dict[str, Any]]
    # Failure-injection knobs for PostgresSyncStore tests. When set, the next
    # workspace_records INSERT commits this "winner" row and then raises the
    # primary-key UniqueViolation, simulating a concurrent first push.
    sync_insert_race_winner: dict[str, Any] | None
    # When true, workspace_records UPDATEs return no row, simulating the
    # RETURNING invariant breaking.
    sync_update_returns_no_row: bool
    # Orphan-bucket first-seen stamps (bucket_name -> datetime), mirroring the
    # orphan_backup_buckets table.
    orphan_stamps: dict[str, datetime]
    # Self-hosted sharing stores, mirroring the shares / relay_tokens /
    # issued_certs tables. Share rows are dicts keyed by _SHARE_COLUMN_NAMES;
    # token rows carry token_hash / host_id / user_id; cert rows carry
    # workspace_domain / not_after.
    share_rows: list[dict[str, Any]]
    relay_token_rows: list[dict[str, Any]]
    issued_cert_rows: list[dict[str, Any]]
    acme_account_rows: list[dict[str, Any]]
    # Relay fleet inventory rows (mirroring the relays table, keyed like
    # relays._RELAY_COLUMN_NAMES) and per-(share, relay) login stamps
    # (mirroring share_tunnel_logins).
    relay_rows: list[dict[str, Any]]
    share_tunnel_login_rows: list[dict[str, Any]]

    def add_relay(
        self,
        relay_id: str,
        region: str,
        tunnel_endpoint: str,
        ip_address: str = "198.51.100.1",
        instance_name: str = "",
        is_active: bool = True,
        health: str = "healthy",
    ) -> None:
        """Seed a relay row (bypassing the admin endpoint), defaulting to active + healthy."""
        self.relay_rows.append(
            make_relay_row(
                relay_id=relay_id,
                region=region,
                tunnel_endpoint=tunnel_endpoint,
                ip_address=ip_address,
                instance_name=instance_name,
                is_active=is_active,
                health=health,
            )
        )

    def upsert_relay(
        self, relay_id: str, region: str, tunnel_endpoint: str, ip_address: str, instance_name: str
    ) -> None:
        """Mirror the admin endpoint's INSERT ... ON CONFLICT (relay_id) upsert (revive-on-reregister)."""
        for relay_row in self.relay_rows:
            if relay_row["relay_id"] == relay_id:
                relay_row.update(
                    {
                        "region": region,
                        "tunnel_endpoint": tunnel_endpoint,
                        "ip_address": ip_address,
                        "instance_name": instance_name,
                        "is_active": True,
                        "health": "healthy",
                        "consecutive_probe_failures": 0,
                    }
                )
                return
        self.add_relay(relay_id, region, tunnel_endpoint, ip_address, instance_name)

    def relay_tuple(self, relay_row: dict[str, Any]) -> tuple[Any, ...]:
        """Project a relay row into the SELECT column order PostgresRelayStore uses."""
        return (
            relay_row["relay_id"],
            relay_row["region"],
            relay_row["tunnel_endpoint"],
            relay_row["ip_address"],
            relay_row["instance_name"],
            relay_row["is_active"],
            relay_row["health"],
            relay_row["consecutive_probe_failures"],
        )

    def add_share(
        self,
        host_id: str,
        user_label: str,
        region: str,
        workspace_domain: str,
        state: str = "active",
    ) -> None:
        """Seed a share row (bypassing the endpoint), defaulting to active."""
        self.upsert_share(host_id, user_label, region, workspace_domain)
        seeded = self.find_share(host_id, user_label)
        assert seeded is not None
        seeded["state"] = state

    def upsert_share(
        self,
        host_id: str,
        user_label: str,
        region: str,
        workspace_domain: str,
        entry_label: str | None = None,
        workspace_id: str | None = None,
        share_label: str | None = None,
    ) -> None:
        """Mirror the endpoint's INSERT ... ON CONFLICT (host_id, user_id) upsert."""
        existing = self.find_share(host_id, user_label)
        if existing is not None:
            existing["region"] = region
            existing["workspace_domain"] = workspace_domain
            existing["state"] = "active"
            existing["updated_at"] = _SHARE_ROW_UPDATED_AT
            # COALESCE semantics: a caller with no label keeps the recorded one.
            if entry_label is not None:
                existing["entry_label"] = entry_label
            if workspace_id is not None:
                existing["workspace_id"] = workspace_id
            if share_label is not None:
                existing["share_label"] = share_label
            return
        self.share_rows.append(
            {
                "host_id": host_id,
                "user_id": user_label,
                "region": region,
                "workspace_domain": workspace_domain,
                "state": "active",
                "created_at": _SHARE_ROW_CREATED_AT,
                "updated_at": _SHARE_ROW_CREATED_AT,
                "last_tunnel_login_at": None,
                "entry_label": entry_label,
                "workspace_id": workspace_id,
                "share_label": share_label,
            }
        )

    def find_share(self, host_id: str, user_label: str) -> dict[str, Any] | None:
        for share in self.share_rows:
            if share["host_id"] == host_id and share["user_id"] == user_label:
                return share
        return None

    def share_tuple(self, share: dict[str, Any]) -> tuple[Any, ...]:
        """Project a stored share row into the SELECT column order PostgresShareStore uses."""
        return tuple(share.get(name) for name in _SHARE_COLUMN_NAMES)

    def add_paid_domain(self, domain: str, is_paid: bool = True) -> None:
        """Seed a paid-domains row (lowercased), defaulting to active."""
        self.paid_domains[domain.lower()] = {
            "is_paid": is_paid,
            "created_at": _PAID_ENTRY_CREATED_AT,
            "updated_at": _PAID_ENTRY_UPDATED_AT,
        }

    def add_paid_email(self, email: str, is_paid: bool = True) -> None:
        """Seed a paid-emails row (lowercased), defaulting to active."""
        self.paid_emails[email.lower()] = {
            "is_paid": is_paid,
            "created_at": _PAID_ENTRY_CREATED_AT,
            "updated_at": _PAID_ENTRY_UPDATED_AT,
        }

    def list_paid_entries(self, store: dict[str, dict[str, Any]], paid_only: bool) -> list[tuple[Any, ...]]:
        """Return ``(value, is_paid, created_at, updated_at)`` rows, sorted by value."""
        return [
            (value, entry["is_paid"], entry["created_at"], entry["updated_at"])
            for value, entry in sorted(store.items())
            if entry["is_paid"] or not paid_only
        ]

    def activate_paid_entry(self, store: dict[str, dict[str, Any]], value: str) -> None:
        """Upsert ``value`` to is_paid=true, keeping created_at on reactivation."""
        existing = store.get(value)
        store[value] = {
            "is_paid": True,
            "created_at": existing["created_at"] if existing else _PAID_ENTRY_CREATED_AT,
            "updated_at": _PAID_ENTRY_UPDATED_AT,
        }

    def deactivate_paid_entry(self, store: dict[str, dict[str, Any]], value: str) -> None:
        """Soft-delete ``value`` (is_paid=false). No-op when absent."""
        existing = store.get(value)
        if existing is not None:
            existing["is_paid"] = False
            existing["updated_at"] = _PAID_ENTRY_UPDATED_AT

    def add_box(
        self,
        server_id: UUID,
        public_address: str = "10.9.9.9",
        lima_service_user: str = "limahost",
        box_host_public_key: str = "ssh-ed25519 AAAA boxkey",
        slot_count: int = 6,
        status: str = "ready",
        region: str = "vin",
    ) -> dict[str, Any]:
        """Seed a bare_metal_servers row for stop/start supervisor tests."""
        box = {
            "id": server_id,
            "public_address": public_address,
            "lima_service_user": lima_service_user,
            "box_host_public_key": box_host_public_key,
            "slot_count": slot_count,
            "status": status,
            "region": region,
        }
        self.box_rows.append(box)
        return box

    def find_box_row(self, raw_id: Any) -> dict[str, Any] | None:
        server_id = UUID(raw_id) if isinstance(raw_id, str) else raw_id
        for box in self.box_rows:
            if box["id"] == server_id:
                return box
        return None

    def box_tuple(self, box: dict[str, Any]) -> tuple[Any, ...]:
        """Project a box row into the SELECT column order stop_start uses."""
        return (
            box["id"],
            box["public_address"],
            box["lima_service_user"],
            box["box_host_public_key"],
            box["slot_count"],
        )

    def find_pool_row(self, raw_id: Any) -> "FakePoolRow | None":
        row_id = UUID(raw_id) if isinstance(raw_id, str) else raw_id
        for row in self.pool_rows:
            if row.host_id == row_id:
                return row
        return None

    def workspace_info_tuple(self, row: "FakePoolRow") -> tuple[Any, ...]:
        """Project a row into workspaces.py's _WORKSPACE_SELECT_COLUMNS order."""
        return (
            row.host_id,
            row.status,
            row.vps_address,
            row.ssh_port,
            row.ssh_user,
            row.container_ssh_port,
            row.agent_id,
            row.host_id_str,
            row.host_name,
            _row_attributes(row),
            row.leased_at,
            row.stop_requested_at,
            row.stopped_at,
            row.transition_error,
            row.outer_host_public_key,
            row.container_host_public_key,
        )

    def workspace_supervisor_tuple(self, row: "FakePoolRow") -> tuple[Any, ...]:
        """Project a row into stop_start's _WORKSPACE_ROW_SELECT column order."""
        return (
            row.host_id,
            row.status,
            row.leased_to_user,
            row.host_id_str,
            row.vps_address,
            row.ssh_port,
            row.ssh_user,
            row.container_ssh_port,
            row.bare_metal_server_id,
            row.lima_instance_name,
            row.lima_disk_name,
            row.region,
            row.stop_requested_at,
            row.artifact_manifest,
            row.wrapped_dek,
            row.artifact_generation,
            row.transition_id,
            row.transition_failure_count,
        )

    def run_box_command_fake(
        self, box: Any, command: str, input_text: str | None = None, timeout_seconds: float = 0
    ) -> tuple[int, str, str]:
        """Pattern-matched stand-in for stop_start._run_box_command."""
        self.box_command_log.append(command)
        if self.box_command_callback is not None:
            self.box_command_callback(command)
        if self.box_command_should_fail_matching and self.box_command_should_fail_matching in command:
            return 1, "", "injected failure"
        if "age-keygen" in command:
            return 0, self.age_keygen_output, ""
        if '/status"' in command and command.startswith("cat"):
            text = (
                self.transfer_status_sequence[0]
                if len(self.transfer_status_sequence) == 1
                else self.transfer_status_sequence.pop(0)
            )
            return 0, text, ""
        # The restore rollback embeds a ``kill -0`` liveness wait of its own, so
        # it is matched before the transfer-alive probe.
        if CLEANUP_DELETE_FAILED_MARKER in command:
            return 0, "", (f"{CLEANUP_DELETE_FAILED_MARKER}\n" if self.cleanup_vm_survives else "")
        if "kill -0" in command:
            return (0 if self.transfer_alive else 1), "", ""
        if "reserve.sh" in command:
            return self.reserve_rc, self.reserve_stdout, self.reserve_stderr
        if command.startswith("[ -d "):
            return (0 if self.vm_exists_on_origin else 1), "", ""
        return 0, "", ""

    def write_box_file_fake(self, box: Any, instance_name: str, filename: str, content: str) -> None:
        self.box_file_writes[f"{instance_name}/{filename}"] = content

    def sleep_fake(self, seconds: float) -> None:
        if self.sleep_callback is not None:
            self.sleep_callback(seconds)

    def read_storage_config_fake(self) -> Any:
        if self.storage_config is None:
            raise MissingStorageConfigError("WORKSPACE_STORAGE_BUCKET")
        return self.storage_config

    def is_storage_configured_fake(self) -> bool:
        return self.storage_config is not None

    def delete_prefix_fake(self, config: Any, prefix: str) -> int:
        if self.delete_prefix_should_fail:
            raise OSError(f"simulated storage outage deleting prefix {prefix}")
        self.deleted_prefixes.append(prefix)
        return 0

    def record_spawned_supervisor(self, host_db_id: str, transition_id: str) -> None:
        self.spawned_supervisors.append(host_db_id)
        self.spawned_supervisor_tokens.append((host_db_id, transition_id))

    def install_on_app_module(self, app_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Swap DB and SSH functions on their owning modules with fakes.

        Uses the same single-loop-setattr pattern as FakeSuperTokensBackend to
        minimize the test-patching ratchet count. The DB connection factory is
        patched on the ``db`` module and the SSH seams on the ``hosts`` module
        (every caller resolves these through the module attribute). ``app_mod``
        is unused here; it is accepted only so both ``Fake*Backend`` install
        helpers share one calling convention.
        """
        fakes: list[tuple[Any, str, Any]] = [
            (db_mod, "get_pool_db_connection", self.get_connection),
            (hosts_module, "_append_authorized_key", self.append_authorized_key),
            (hosts_module, "_write_files_on_container", self.write_files_on_container),
            (hosts_module, "_adopt_workspace_on_container", self.adopt_workspace_on_container),
            (hosts_module, "_start_workspace_agent_on_container", self.start_workspace_agent_on_container),
            (hosts_module, "clean_up_slice_on_box", self.clean_up_slice_on_box),
            (stop_start_module, "_run_box_command", self.run_box_command_fake),
            (stop_start_module, "_write_box_file", self.write_box_file_fake),
            (stop_start_module, "_sleep", self.sleep_fake),
            (stop_start_module.spawner, "hook", self.record_spawned_supervisor),
            (connector_storage_module, "read_storage_config", self.read_storage_config_fake),
            (connector_storage_module, "is_storage_configured", self.is_storage_configured_fake),
            (connector_storage_module, "delete_prefix", self.delete_prefix_fake),
        ]
        for target_module, name, fake in fakes:
            monkeypatch.setattr(target_module, name, fake)

    def get_connection(self) -> FakeConnection:
        return _make_fake_connection(self)

    def find_sync_record(self, user_id: str, host_id: str) -> dict[str, Any] | None:
        """Return the workspace-record row whose host_id column matches, or None."""
        for row in self.sync_record_rows:
            if row["user_id"] == user_id and row["host_id"] == host_id:
                return row
        return None

    def find_sync_record_by_agent(self, user_id: str, agent_id: str) -> dict[str, Any] | None:
        """Return the workspace-record row for (user_id, agent_id) -- the primary key -- or None."""
        for row in self.sync_record_rows:
            if row["user_id"] == user_id and row["agent_id"] == agent_id:
                return row
        return None

    def find_sync_record_by_short_name(self, user_id: str, short_name: str) -> dict[str, Any] | None:
        """Return a row whose host_id OR agent_id matches (the bucket reservation checks)."""
        for row in self.sync_record_rows:
            if row["user_id"] == user_id and short_name in (row["host_id"], row["agent_id"]):
                return row
        return None

    def sync_record_tuple(self, row: dict[str, Any]) -> tuple[Any, ...]:
        """Project a stored row into the SELECT column order PostgresSyncStore uses."""
        # record_format mirrors the migration's NOT NULL DEFAULT 1: rows seeded
        # by older fixtures (no explicit value) read back as format 1.
        return tuple(
            row.get(name, 1) if name == "record_format" else row.get(name) for name in _WORKSPACE_RECORD_COLUMN_NAMES
        )

    def insert_sync_record(self, params: tuple[Any, ...]) -> dict[str, Any]:
        """Simulate the workspace_records INSERT, including its unique-violation modes."""
        (
            user_id,
            host_id,
            agent_id,
            display_name,
            color,
            provider_kind,
            hosting_device_id,
            device_label,
            state,
            restored_from_host_id,
            backup_bucket,
            encrypted_secrets,
            revision,
            record_format,
            # The trailing state param feeds the destroyed_at CASE expression.
            _state_for_destroyed_at,
        ) = params
        if self.sync_insert_race_winner is not None:
            winner = dict(self.sync_insert_race_winner)
            self.sync_insert_race_winner = None
            winner.setdefault("created_at", _SYNC_ROW_CREATED_AT)
            winner.setdefault("updated_at", _SYNC_ROW_CREATED_AT)
            self.sync_record_rows.append(winner)
            raise psycopg2.errors.UniqueViolation("concurrent insert won the primary key")
        if self.find_sync_record_by_agent(user_id, agent_id) is not None:
            raise psycopg2.errors.UniqueViolation(f"duplicate primary key ({user_id}, {agent_id})")
        row = {
            "user_id": user_id,
            "host_id": host_id,
            "agent_id": agent_id,
            "display_name": display_name,
            "color": color,
            "provider_kind": provider_kind,
            "hosting_device_id": hosting_device_id,
            "device_label": device_label,
            "state": state,
            "restored_from_host_id": restored_from_host_id,
            "backup_bucket": backup_bucket,
            "encrypted_secrets": _adapted_bytes(encrypted_secrets),
            "revision": revision,
            "record_format": record_format,
            "created_at": _SYNC_ROW_CREATED_AT,
            "updated_at": _SYNC_ROW_CREATED_AT,
            "destroyed_at": datetime.now(timezone.utc) if state == "destroyed" else None,
        }
        self.sync_record_rows.append(row)
        return row

    def insert_lease_record_stub(self, params: tuple[Any, ...]) -> None:
        """Simulate the lease-time metadata-only record INSERT ... ON CONFLICT DO NOTHING."""
        user_id, host_id, agent_id, display_name, provider_kind = params
        if self.find_sync_record_by_agent(user_id, agent_id) is not None:
            return
        self.add_workspace_record(
            user_id=user_id,
            host_id=host_id,
            agent_id=agent_id,
            display_name=display_name,
            provider_kind=provider_kind,
        )

    def add_workspace_record(
        self,
        *,
        user_id: str,
        host_id: str,
        agent_id: str,
        display_name: str = "ws",
        provider_kind: str = "imbue_cloud_x",
        state: str = "active",
        destroyed_at: datetime | None = None,
        # Revision 1 is the untouched lease stub; a client's first push lands
        # at 2, which is what makes a release tombstone the record instead of
        # deleting it.
        revision: int = 1,
    ) -> dict[str, Any]:
        """Seed a metadata-only workspace record (the lease stub's shape) and return the row."""
        row = {
            "user_id": user_id,
            "host_id": host_id,
            "agent_id": agent_id,
            "display_name": display_name,
            "color": None,
            "provider_kind": provider_kind,
            "hosting_device_id": None,
            "device_label": "",
            "state": state,
            "restored_from_host_id": None,
            "backup_bucket": None,
            "encrypted_secrets": None,
            "revision": revision,
            "record_format": 1,
            "created_at": _SYNC_ROW_CREATED_AT,
            "updated_at": _SYNC_ROW_CREATED_AT,
            "destroyed_at": destroyed_at,
        }
        self.sync_record_rows.append(row)
        return row

    def update_sync_record(self, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        """Simulate the workspace_records preserve-on-absent UPDATE; returns the updated row or None.

        The real statement's SET clause is dynamic (only the columns the push
        named appear), so this parses the clause list from the query instead
        of destructuring a fixed parameter tuple.
        """
        clauses = _split_sql_set_clauses(query)
        column_updates: dict[str, Any] = {}
        state_for_destroyed_at: Any = None
        param_idx = 0
        for clause in clauses:
            column = clause.split("=", 1)[0].strip()
            placeholder_count = clause.count("%s")
            clause_values = params[param_idx : param_idx + placeholder_count]
            param_idx += placeholder_count
            if column == "updated_at":
                column_updates["updated_at"] = _SYNC_ROW_UPDATED_AT
            elif column == "destroyed_at":
                state_for_destroyed_at = clause_values[0]
            elif column == "encrypted_secrets":
                column_updates["encrypted_secrets"] = _adapted_bytes(clause_values[0])
            else:
                column_updates[column] = clause_values[0]
        user_id, agent_id = params[param_idx], params[param_idx + 1]
        if self.sync_update_returns_no_row:
            return None
        row = self.find_sync_record_by_agent(user_id, agent_id)
        if row is None:
            return None
        # Mirror the SQL CASE: stamp on the destroyed transition (keeping an
        # existing stamp), clear on resurrection to active.
        destroyed_at = (
            (row.get("destroyed_at") or datetime.now(timezone.utc)) if state_for_destroyed_at == "destroyed" else None
        )
        row.update(column_updates)
        row["destroyed_at"] = destroyed_at
        return row

    def scrub_sync_secrets(self, user_id: str) -> int:
        """Null out every non-null encrypted_secrets for the user; returns the row count."""
        scrubbed = 0
        for row in self.sync_record_rows:
            if row["user_id"] == user_id and row["encrypted_secrets"] is not None:
                row["encrypted_secrets"] = None
                row["updated_at"] = _SYNC_ROW_UPDATED_AT
                scrubbed += 1
        return scrubbed

    def clean_up_slice_on_box(
        self,
        host_db_id: Any,
        bare_metal_server_id: Any,
        lima_instance_name: str | None,
        lima_disk_name: str | None,
    ) -> None:
        """Record a slice teardown (the real box SSH is not exercised in unit tests)."""
        if self.slice_teardown_should_fail:
            raise PoolHostCleanupError(f"simulated slice teardown failure for {host_db_id}")
        self.slice_teardowns.append((host_db_id, bare_metal_server_id, lima_instance_name, lima_disk_name))

    def append_authorized_key(
        self,
        host: str,
        port: int,
        user: str,
        management_key_pem: str,
        public_key_to_add: str,
        expected_host_public_key: str,
    ) -> None:
        self.append_key_calls.append(
            (host, port, user, management_key_pem, public_key_to_add, expected_host_public_key)
        )
        if host in self.append_key_failure_addresses:
            raise paramiko.SSHException(f"injected key injection failure for {host}")

    def adopt_workspace_on_container(
        self,
        host: str,
        port: int,
        user: str,
        management_key_pem: str,
        expected_host_public_key: str,
        agent_id: str,
        host_name: str,
        display_name: str,
        connector_url: str,
    ) -> None:
        """Record a web-claim adopt instead of touching a container over SSH."""
        if self.adopt_should_fail:
            raise paramiko.SSHException("injected adopt failure")
        self.adopted_containers.append((host, port, agent_id, host_name, display_name, connector_url))

    def start_workspace_agent_on_container(
        self,
        host: str,
        port: int,
        user: str,
        management_key_pem: str,
        expected_host_public_key: str,
    ) -> None:
        """Record a web-claim agent start instead of running the start script over SSH."""
        if self.agent_start_should_fail:
            raise paramiko.SSHException("injected agent start failure")
        self.started_agent_containers.append((host, port))

    def write_files_on_container(
        self,
        host: str,
        port: int,
        user: str,
        management_key_pem: str,
        files_by_remote_path: dict[str, str],
        expected_host_public_key: str,
        seed_only_remote_paths: frozenset[str],
    ) -> None:
        """Capture a server-side share-materials injection instead of SSHing."""
        self.written_container_files.append((host, port, files_by_remote_path))
        self.written_container_seed_only_paths.append(frozenset(seed_only_remote_paths))

    def add_available_host(
        self,
        host_id: UUID,
        version: str,
        vps_address: str = "203.0.113.10",
        ssh_port: int = 22,
        ssh_user: str = "root",
        container_ssh_port: int = 2222,
        agent_id: str = "agent-abc123",
        host_id_str: str = "host-xyz",
        host_name: str | None = None,
        region: str | None = None,
        outer_host_public_key: str | None = _FAKE_OUTER_HOST_PUBLIC_KEY,
        container_host_public_key: str | None = _FAKE_CONTAINER_HOST_PUBLIC_KEY,
        attributes: dict[str, Any] | None = None,
    ) -> FakePoolRow:
        """Add an available host to the in-memory pool."""
        row = _make_pool_row(
            host_id=host_id,
            vps_address=vps_address,
            agent_id=agent_id,
            host_id_str=host_id_str,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            container_ssh_port=container_ssh_port,
            version=version,
            host_name=host_name,
            region=region,
            outer_host_public_key=outer_host_public_key,
            container_host_public_key=container_host_public_key,
        )
        if attributes is not None:
            row.attributes = attributes
        self.pool_rows.append(row)
        return row

    def add_leased_host(
        self,
        host_id: UUID,
        version: str,
        leased_to_user: str,
        vps_address: str = "203.0.113.10",
        ssh_port: int = 22,
        ssh_user: str = "root",
        container_ssh_port: int = 2222,
        agent_id: str = "agent-abc123",
        host_id_str: str = "host-xyz",
        host_name: str | None = None,
    ) -> FakePoolRow:
        """Add a leased host to the in-memory pool."""
        row = _make_pool_row(
            host_id=host_id,
            vps_address=vps_address,
            agent_id=agent_id,
            host_id_str=host_id_str,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            container_ssh_port=container_ssh_port,
            version=version,
            status="leased",
            leased_to_user=leased_to_user,
            leased_at="2026-01-01T00:00:00+00:00",
            host_name=host_name,
        )
        self.pool_rows.append(row)
        return row

    def add_removing_host(
        self,
        host_id: UUID,
        version: str,
        leased_to_user: str = "some-user",
        vps_address: str = "203.0.113.10",
        agent_id: str = "agent-abc123",
        host_id_str: str = "host-xyz",
    ) -> FakePoolRow:
        """Add a host already marked 'removing' (an interrupted release) to the pool."""
        row = _make_pool_row(
            host_id=host_id,
            vps_address=vps_address,
            agent_id=agent_id,
            host_id_str=host_id_str,
            ssh_port=22,
            ssh_user="root",
            container_ssh_port=2222,
            version=version,
            status="removing",
            leased_to_user=leased_to_user,
            leased_at="2026-01-01T00:00:00+00:00",
        )
        row.released_at = "2026-01-02T00:00:00+00:00"
        # An interrupted release of a leased slice row retains its box link and
        # lima names, which is what makes its VM teardown retryable.
        row.bare_metal_server_id = UUID("00000000-0000-0000-0000-0000000000b1")
        row.lima_instance_name = f"mngr-slice-test-{host_id.hex}"
        row.lima_disk_name = f"mngr-slice-test-{host_id.hex}-data"
        self.pool_rows.append(row)
        return row

    def add_leased_workspace(
        self,
        *,
        suffix: str,
        leased_to_user: str,
        record_user_id: str,
        record_state: str = "active",
        destroyed_at: datetime | None = None,
        record_revision: int = 1,
    ) -> FakePoolRow:
        """Seed a leased pool row plus its workspace record, both keyed by ``suffix``.

        The row id is ``00000000-0000-0000-0000-0000000000<suffix>`` (two hex
        chars) and the workspace/host ids are ``agent-<suffix>`` /
        ``host-<suffix>``. ``record_user_id`` is separate from the lease's
        ``leased_to_user`` prefix so a test can seed another user's record for
        the same workspace.
        """
        row = self.add_leased_host(
            host_id=UUID(f"00000000-0000-0000-0000-0000000000{suffix}"),
            version="v0.1.0",
            leased_to_user=leased_to_user,
            agent_id=f"agent-{suffix}",
            host_id_str=f"host-{suffix}",
        )
        self.add_workspace_record(
            user_id=record_user_id,
            host_id=f"host-{suffix}",
            agent_id=f"agent-{suffix}",
            state=record_state,
            destroyed_at=destroyed_at,
            revision=record_revision,
        )
        return row


def make_fake_pool_backend() -> FakePoolBackend:
    """Construct an empty in-memory pool backend (no pool rows, empty paid lists)."""
    backend = FakePoolBackend()
    backend.pool_rows = []
    backend.append_key_calls = []
    backend.append_key_failure_addresses = set()
    backend.written_container_files = []
    backend.written_container_seed_only_paths = []
    backend.adopted_containers = []
    backend.adopt_should_fail = False
    backend.started_agent_containers = []
    backend.agent_start_should_fail = False
    backend.slice_teardowns = []
    backend.slice_teardown_should_fail = False
    backend.paid_domains = {}
    backend.paid_emails = {}
    backend.sync_record_rows = []
    backend.sync_bundle_by_user = {}
    backend.sync_insert_race_winner = None
    backend.sync_update_returns_no_row = False
    backend.orphan_stamps = {}
    backend.share_rows = []
    backend.relay_token_rows = []
    backend.issued_cert_rows = []
    backend.acme_account_rows = []
    # The standard test fleet: one relay per region, mirroring what the
    # provisioning flow registers. Tests exercising an empty or altered fleet
    # mutate ``relay_rows`` directly.
    backend.relay_rows = []
    backend.share_tunnel_login_rows = []
    backend.add_relay(_RELAY_ID_US1, "us1", _RELAY_ENDPOINT_US1, ip_address="198.51.100.1")
    backend.add_relay(_RELAY_ID_US2, "us2", _RELAY_ENDPOINT_US2, ip_address="198.51.100.2")
    backend.box_rows = []
    backend.storage_config = None
    backend.deleted_prefixes = []
    backend.delete_prefix_should_fail = False
    backend.box_command_log = []
    backend.box_file_writes = {}
    backend.transfer_status_sequence = [
        "STAGE=uploaded\nFINISHED=1\nSHA_DISK=aa11\nBYTES_DISK=100\nSHA_DATADISK=bb22\nBYTES_DATADISK=50\nSHA_META=cc33\nBYTES_META=10\n"
    ]
    backend.transfer_alive = False
    backend.vm_exists_on_origin = True
    backend.age_keygen_output = (
        "# created: 2026-01-01T00:00:00Z\n# public key: age1qtestrecipient\nAGE-SECRET-KEY-1TESTIDENTITY\n"
    )
    backend.reserve_rc = 0
    backend.reserve_stdout = "MNGR_RESTORE_RESERVED 23000 23001\n"
    backend.reserve_stderr = ""
    backend.cleanup_vm_survives = False
    backend.spawned_supervisors = []
    backend.spawned_supervisor_tokens = []
    backend.box_command_should_fail_matching = None
    backend.box_command_callback = None
    backend.sleep_callback = None
    return backend


def make_storage_config(retention_seconds: int = 0) -> "connector_storage_module.StorageConfig":
    """A storage config for stop/start tests (retention 0 so stops finalize immediately)."""
    return connector_storage_module.StorageConfig(
        s3_endpoint="https://s3.test.example",
        s3_region="us-east-va",
        access_key_id="testaccess",
        secret_access_key="testsecret",
        bucket="mngr-workspaces-test",
        kek_base64=base64.b64encode(b"0" * 32).decode("ascii"),
        retention_seconds=retention_seconds,
    )


class InMemorySyncStore:
    """In-memory SyncStore implementation for testing the workspace-sync endpoints.

    Mirrors PostgresSyncStore's semantics: rows keyed by (user_id, agent_id)
    -- the workspace id -- with host_id as a mutable column, CAS on revision
    for updates, scrub, and the per-user key bundle. Secrets are raw bytes.
    """

    def __init__(self) -> None:
        self.records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        self.bundle_by_user_id: dict[str, dict[str, Any]] = {}
        self._created_counter = 0

    def _next_timestamp(self) -> str:
        self._created_counter += 1
        return f"2026-01-01T00:00:{self._created_counter:02d}+00:00"

    def _encode_secrets(self, record: dict[str, Any]) -> dict[str, Any]:
        encoded = dict(record)
        secrets_bytes = record.get("encrypted_secrets")
        encoded["encrypted_secrets"] = (
            base64.b64encode(secrets_bytes).decode("ascii") if secrets_bytes is not None else None
        )
        destroyed_at = record.get("destroyed_at")
        encoded["destroyed_at"] = str(destroyed_at) if destroyed_at is not None else None
        return encoded

    def list_records(self, user_id: str) -> list[dict[str, Any]]:
        rows = [self._encode_secrets(record) for (uid, _), record in self.records_by_key.items() if uid == user_id]
        return sorted(rows, key=lambda record: record["created_at"])

    def put_record(self, user_id: str, record: dict[str, Any], sent_fields: AbstractSet[str]) -> dict[str, Any]:
        key = (user_id, record["agent_id"])
        existing = self.records_by_key.get(key)
        if existing is not None and record.get("record_format", 1) < existing.get("record_format", 1):
            raise SyncRecordFormatTooNewError(self._encode_secrets(existing))
        if existing is not None and record["revision"] != existing["revision"] + 1:
            raise SyncRevisionConflictError(self._encode_secrets(existing))
        # Preserve-on-absent, mirroring PostgresSyncStore: an update writes
        # only the updatable fields the push named; absent fields keep their
        # stored values.
        if existing is not None:
            stored = dict(existing)
            for field_name in UPDATABLE_RECORD_COLUMNS:
                if field_name in sent_fields:
                    stored[field_name] = record[field_name]
            stored["revision"] = record["revision"]
        else:
            stored = dict(record)
        stored["created_at"] = existing["created_at"] if existing is not None else self._next_timestamp()
        stored["updated_at"] = self._next_timestamp()
        # Mirror the server-side destroyed_at stamping: set on the destroyed
        # transition (keeping an existing stamp), cleared on resurrection.
        prior_destroyed_at = existing.get("destroyed_at") if existing is not None else None
        stored["destroyed_at"] = (
            (prior_destroyed_at or datetime.now(timezone.utc)) if record["state"] == "destroyed" else None
        )
        self.records_by_key[key] = stored
        return self._encode_secrets(stored)

    def delete_record(self, user_id: str, host_id: str) -> None:
        self.records_by_key = {
            key: record
            for key, record in self.records_by_key.items()
            if not (key[0] == user_id and record["host_id"] == host_id)
        }

    def delete_record_by_workspace(self, user_id: str, workspace_id: str) -> None:
        self.records_by_key.pop((user_id, workspace_id), None)

    def list_destroyed_records_before(self, cutoff: datetime) -> list[dict[str, Any]]:
        rows = [
            {
                "user_id": uid,
                "host_id": record["host_id"],
                "agent_id": agent_id,
                "backup_bucket": record.get("backup_bucket"),
                "destroyed_at": record["destroyed_at"],
            }
            for (uid, agent_id), record in self.records_by_key.items()
            if record["state"] == "destroyed"
            and record.get("destroyed_at") is not None
            and record["destroyed_at"] < cutoff
        ]
        return sorted(rows, key=lambda row: row["destroyed_at"])

    def any_record_references_backup_bucket(
        self, user_id_prefix: str, bucket_name: str, short_name: str, excluding_workspace_id: str | None = None
    ) -> bool:
        return any(
            (record.get("backup_bucket") == bucket_name or short_name in (record["host_id"], key[1]))
            and derive_user_id_prefix(key[0]) == user_id_prefix
            and (excluding_workspace_id is None or key[1] != excluding_workspace_id)
            for key, record in self.records_by_key.items()
        )

    def scrub_secrets(self, user_id: str) -> int:
        scrubbed = 0
        for (uid, _), record in self.records_by_key.items():
            if uid == user_id and record.get("encrypted_secrets") is not None:
                record["encrypted_secrets"] = None
                record["updated_at"] = self._next_timestamp()
                scrubbed += 1
        return scrubbed

    def get_bundle(self, user_id: str) -> dict[str, Any] | None:
        bundle = self.bundle_by_user_id.get(user_id)
        if bundle is None:
            return None
        encoded = dict(bundle)
        encoded["kdf_salt"] = base64.b64encode(bundle["kdf_salt"]).decode("ascii")
        encoded["wrapped_dek"] = base64.b64encode(bundle["wrapped_dek"]).decode("ascii")
        return encoded

    def put_bundle(self, user_id: str, bundle: dict[str, Any]) -> None:
        stored = dict(bundle)
        stored["updated_at"] = self._next_timestamp()
        self.bundle_by_user_id[user_id] = stored

    def put_bundle_if_absent(self, user_id: str, bundle: dict[str, Any]) -> bool:
        if user_id in self.bundle_by_user_id:
            return False
        self.put_bundle(user_id, bundle)
        return True

    def delete_bundle(self, user_id: str) -> None:
        self.bundle_by_user_id.pop(user_id, None)


def make_fake_sync_store() -> InMemorySyncStore:
    """Construct an empty in-memory SyncStore for tests."""
    return InMemorySyncStore()


class InMemoryOrphanBucketStore:
    """In-memory OrphanBucketStore for testing the backup-retention reaper."""

    def __init__(self) -> None:
        self.stamps_by_bucket: dict[str, datetime] = {}

    def get_first_seen(self, bucket_name: str) -> datetime | None:
        return self.stamps_by_bucket.get(bucket_name)

    def get_or_record_first_seen(self, bucket_name: str) -> datetime:
        return self.stamps_by_bucket.setdefault(bucket_name, datetime.now(timezone.utc))

    def delete_stamp(self, bucket_name: str) -> None:
        self.stamps_by_bucket.pop(bucket_name, None)


def make_fake_orphan_bucket_store() -> InMemoryOrphanBucketStore:
    """Construct an empty in-memory OrphanBucketStore for tests."""
    return InMemoryOrphanBucketStore()


# ---------------------------------------------------------------------------
# Plans + entitlements fakes
# ---------------------------------------------------------------------------


# Canonical plan values matching the committed deploy.toml [plans] blocks.
FREE_PLAN_VALUES: Final[dict[str, float]] = {
    "max_remote_workspaces": 1,
    "max_total_workspaces": 5,
    "max_buckets": 5,
    "max_total_bucket_bytes": 25 * 1024**3,
    "monthly_llm_spend_usd": 0.0,
    "max_active_synced_workspaces": 200,
}
EXPLORER_PLAN_VALUES: Final[dict[str, float]] = {
    "max_remote_workspaces": 2,
    "max_total_workspaces": 10,
    "max_buckets": 5,
    "max_total_bucket_bytes": 50 * 1024**3,
    "monthly_llm_spend_usd": 0.0,
    "max_active_synced_workspaces": 200,
}
ALLY_PLAN_VALUES: Final[dict[str, float]] = {
    "max_remote_workspaces": 10,
    "max_total_workspaces": 50,
    "max_buckets": 20,
    "max_total_bucket_bytes": 500 * 1024**3,
    "monthly_llm_spend_usd": 1000.0,
    "max_active_synced_workspaces": 200,
}


class InMemoryEntitlementsStore:
    """In-memory EntitlementsStore for testing plans + per-account quota rows.

    Set ``raise_on_insert`` to exercise the signup plan recorder's fail-open
    path (the exception is raised by ``insert_entitlements_if_absent``).
    """

    def __init__(self) -> None:
        # plan_name -> {plan_name, <quota columns>}
        self.plans_by_name: dict[str, dict[str, Any]] = {}
        # user_id -> {user_id, user_id_prefix, plan_name, <quota columns>}
        self.rows_by_user_id: dict[str, dict[str, Any]] = {}
        self.raise_on_insert: Exception | None = None

    def seed_plan(self, plan_name: str, values: dict[str, float]) -> None:
        self.plans_by_name[plan_name] = {"plan_name": plan_name, **values}

    def get_plan(self, plan_name: str) -> dict[str, Any] | None:
        row = self.plans_by_name.get(plan_name)
        return dict(row) if row is not None else None

    def list_plans(self) -> list[dict[str, Any]]:
        return [dict(row) for _, row in sorted(self.plans_by_name.items())]

    def get_entitlements(self, user_id: str) -> dict[str, Any] | None:
        row = self.rows_by_user_id.get(user_id)
        return dict(row) if row is not None else None

    def get_entitlements_by_prefix(self, user_id_prefix: str) -> dict[str, Any] | None:
        for row in self.rows_by_user_id.values():
            if row["user_id_prefix"] == user_id_prefix:
                return dict(row)
        return None

    def insert_entitlements_if_absent(self, row: dict[str, Any]) -> None:
        if self.raise_on_insert is not None:
            raise self.raise_on_insert
        self.rows_by_user_id.setdefault(row["user_id"], dict(row))

    def update_entitlements(self, user_id: str, values: dict[str, Any]) -> None:
        row = self.rows_by_user_id.get(user_id)
        if row is not None:
            row.update(values)


def make_fake_entitlements_store() -> InMemoryEntitlementsStore:
    """Construct an in-memory entitlements store pre-seeded with the committed plans."""
    store = InMemoryEntitlementsStore()
    store.seed_plan("free", FREE_PLAN_VALUES)
    store.seed_plan("explorer", EXPLORER_PLAN_VALUES)
    store.seed_plan("ally", ALLY_PLAN_VALUES)
    return store


# ---------------------------------------------------------------------------
# R2 cleanup-grant fakes
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def noop_enforcement_lock(owner_user_id: str) -> Iterator[None]:
    """Lock stand-in for direct run_r2_quota_sweep calls (no DB in unit tests)."""
    del owner_user_id
    yield


class InMemoryGrantStore:
    """In-memory GrantStore for testing cleanup grants.

    Time is a bare minute counter (``now_minutes``): tests advance it to
    expire grants, and timestamps in stored rows are plain integers rendered
    to strings by the endpoints.
    """

    def __init__(self) -> None:
        self.grants_by_id: dict[int, dict[str, Any]] = {}
        self.now_minutes = 0
        self._next_grant_id = 1

    def create_grant(
        self, user_id: str, user_id_prefix: str, baseline_bytes: int, expiry_minutes: int
    ) -> dict[str, Any]:
        grant = {
            "grant_id": self._next_grant_id,
            "user_id": user_id,
            "user_id_prefix": user_id_prefix,
            "baseline_bytes": baseline_bytes,
            "granted_at": self.now_minutes,
            "expires_at": self.now_minutes + expiry_minutes,
            "settled_at": None,
            "settled_bytes": None,
            "is_decreased": None,
        }
        self.grants_by_id[self._next_grant_id] = grant
        self._next_grant_id += 1
        return dict(grant)

    def get_active_grant(self, user_id: str) -> dict[str, Any] | None:
        for grant in sorted(self.grants_by_id.values(), key=lambda g: -int(g["granted_at"])):
            if grant["user_id"] == user_id and grant["settled_at"] is None and grant["expires_at"] > self.now_minutes:
                return dict(grant)
        return None

    def list_unsettled_grants(self, user_id: str) -> list[dict[str, Any]]:
        return [
            dict(grant)
            for grant in sorted(self.grants_by_id.values(), key=lambda g: int(g["granted_at"]))
            if grant["user_id"] == user_id and grant["settled_at"] is None
        ]

    def list_expired_unsettled_grants(self) -> list[dict[str, Any]]:
        return [
            dict(grant)
            for grant in sorted(self.grants_by_id.values(), key=lambda g: int(g["granted_at"]))
            if grant["settled_at"] is None and grant["expires_at"] <= self.now_minutes
        ]

    def settle_grant(self, grant_id: int, settled_bytes: int, is_decreased: bool) -> None:
        grant = self.grants_by_id.get(grant_id)
        if grant is not None and grant["settled_at"] is None:
            grant["settled_at"] = self.now_minutes
            grant["settled_bytes"] = settled_bytes
            grant["is_decreased"] = is_decreased

    def count_failed_grants_in_window(self, user_id: str, window_hours: int) -> int:
        window_start = self.now_minutes - window_hours * 60
        return sum(
            1
            for grant in self.grants_by_id.values()
            if grant["user_id"] == user_id
            and grant["settled_at"] is not None
            and grant["is_decreased"] is False
            and grant["granted_at"] > window_start
        )


def make_fake_grant_store() -> InMemoryGrantStore:
    """Construct an empty in-memory GrantStore for tests."""
    return InMemoryGrantStore()


# ---------------------------------------------------------------------------
# LiteLLM admin-API fake
# ---------------------------------------------------------------------------


class _FakeLiteLLMResponse:
    """Minimal httpx.Response stand-in exposing .json()."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeLiteLLMBackend:
    """In-memory replacement for the connector's ``litellm_client.litellm_request`` helper.

    Tracks internal users (for the user-level budget upserts) and key
    operations; ``fail_user_writes`` lets tests simulate a LiteLLM outage
    during a budget push (both /user/new and /user/update fail while set). Install by monkeypatching
    ``litellm_client.litellm_request`` to :meth:`request`.
    """

    def __init__(self) -> None:
        self.users_by_id: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.generated_keys: list[dict[str, Any]] = []
        self.keys_by_id: dict[str, dict[str, Any]] = {}
        self.fail_user_writes: bool = False
        self._next_key_idx = 1

    def request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> _FakeLiteLLMResponse:
        self.calls.append((method, path))
        body = json_body or {}
        query = params or {}
        if path == "/user/new":
            if self.fail_user_writes:
                raise HTTPException(status_code=500, detail="LiteLLM error: simulated outage")
            user_id = str(body["user_id"])
            if user_id in self.users_by_id:
                raise HTTPException(status_code=400, detail="LiteLLM error: user already exists")
            self.users_by_id[user_id] = {"user_id": user_id, "spend": 0.0, **body}
            return _FakeLiteLLMResponse({"user_id": user_id})
        if path == "/user/update":
            if self.fail_user_writes:
                raise HTTPException(status_code=500, detail="LiteLLM error: simulated outage")
            user_id = str(body["user_id"])
            self.users_by_id.setdefault(user_id, {"user_id": user_id, "spend": 0.0}).update(body)
            return _FakeLiteLLMResponse({"user_id": user_id})
        if path == "/user/info":
            user = self.users_by_id.get(str(query.get("user_id", "")))
            return _FakeLiteLLMResponse({"user_info": dict(user) if user else None})
        if path == "/key/generate":
            key = {"key": f"sk-fake-{self._next_key_idx}", "spend": 0.0, **body}
            self._next_key_idx += 1
            self.generated_keys.append(key)
            self.keys_by_id[key["key"]] = key
            return _FakeLiteLLMResponse({"key": key["key"]})
        if path == "/key/list":
            wanted_user = str(query.get("user_id", ""))
            return _FakeLiteLLMResponse(
                [
                    {"token": k["key"], **{f: k.get(f) for f in ("key_alias", "user_id", "spend", "max_budget")}}
                    for k in self.keys_by_id.values()
                    if not wanted_user or k.get("user_id") == wanted_user
                ]
            )
        if path == "/key/info":
            key = self.keys_by_id.get(str(query.get("key", "")))
            if key is None:
                raise HTTPException(status_code=404, detail="LiteLLM error: key not found")
            return _FakeLiteLLMResponse({"info": {"token": key["key"], **key}})
        if path == "/key/update":
            key = self.keys_by_id.get(str(body.get("key", "")))
            if key is None:
                raise HTTPException(status_code=404, detail="LiteLLM error: key not found")
            key.update({name: value for name, value in body.items() if name != "key"})
            return _FakeLiteLLMResponse({"status": "updated"})
        if path == "/key/delete":
            for key_id in body.get("keys", []):
                self.keys_by_id.pop(str(key_id), None)
            return _FakeLiteLLMResponse({"status": "deleted"})
        if path in ("/key/block", "/key/unblock"):
            key = self.keys_by_id.get(str(body.get("key", "")))
            if key is None:
                raise HTTPException(status_code=404, detail="LiteLLM error: key not found")
            key["blocked"] = path == "/key/block"
            return _FakeLiteLLMResponse({"token": key["key"], "blocked": key["blocked"]})
        raise HTTPException(status_code=404, detail=f"LiteLLM error: unhandled fake path {path}")


def make_fake_litellm_backend() -> FakeLiteLLMBackend:
    """Construct an empty in-memory LiteLLM admin-API fake."""
    return FakeLiteLLMBackend()


# --- Shared route-test helpers (moved from the pre-split app_test.py) ---


_USER_STUB_TOKEN = "user-stub-jwt"


_USER_STUB_EMAIL = "testuser@example.com"


_USER_STUB_USER_ID = "12345678-1234-5678-1234-567812345678"


# What `pool_hosts.leased_to_user` stores for the stub user, so fakes joining
# leases against workspace records line up.
_USER_STUB_USER_ID_PREFIX = derive_user_id_prefix(_USER_STUB_USER_ID)


_ADMIN_KEY_TEST_VALUE = "admin-key-secret-9f3a2b"


def _user_headers() -> dict[str, str]:
    """Return a Bearer header for a fake SuperTokens user session.

    Paired with ``_make_test_client`` which stubs ``_authenticate_supertokens``
    to recognise ``_USER_STUB_TOKEN`` and return a canned ``UserAuth``.
    """
    return {"Authorization": f"Bearer {_USER_STUB_TOKEN}"}


def _make_quota_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, InMemoryEntitlementsStore, FakeLiteLLMBackend]:
    """Create a TestClient with the FastAPI app plus every quota-relevant fake.

    Sets up the SuperTokens Bearer auth path so tests calling user-authenticated endpoints
    can authenticate with ``_user_headers()`` without needing a real JWT.
    Installs an in-memory paid-list backend seeded with the stub user email,
    an entitlements store pre-seeded with the committed plans (with the stub
    user's SuperTokens ``time_joined`` faked to 0, i.e. pre-cutoff, so the
    stub's lazy plan resolves to ally by default), and a fake LiteLLM admin
    API. The paid-status cache is disabled
    (``MINDS_PAID_LIST_CACHE_TTL_SECONDS=0``) so the module-level cache never
    bleeds between tests.
    """
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://fake-supertokens.example.com")
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "0")
    # ``/keys/create`` embeds the proxy URL in its response (the LiteLLM calls
    # themselves go through the installed fake).
    monkeypatch.setenv("LITELLM_PROXY_URL", "https://fake-litellm.example.com")
    fake_ctx = make_fake_cloudflare_ctx()

    def _stub_supertokens(token: str, check_database: bool = False) -> UserAuth:
        del check_database
        if token != _USER_STUB_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid token")
        # The stub user simulates a fully-verified account (it is paid-listed
        # below, and ally eligibility requires a verified email).
        return UserAuth(user_id_prefix=_USER_STUB_USER_ID_PREFIX, email=_USER_STUB_EMAIL, is_email_verified=True)

    entitlements_store = make_fake_entitlements_store()
    litellm = make_fake_litellm_backend()
    # Single-loop patching (matches the Fake*Backend.install_on_app_module
    # pattern) so the monkeypatch ratchet only counts one occurrence. Each
    # seam is patched on its owning module (call sites resolve seams through
    # the module attribute).
    quota_fakes: list[tuple[object, str, object]] = [
        (cloudflare_mod, "get_cloudflare_ctx", lambda: fake_ctx),
        (auth_mod, "_authenticate_supertokens", _stub_supertokens),
        (entitlements_mod, "get_entitlements_store", lambda: entitlements_store),
        (auth_mod, "get_user_id_from_access_token", lambda token: _USER_STUB_USER_ID),
        (entitlements_mod, "_get_user_time_joined_ms", lambda user_id, user_getter=None: 0),
        (litellm_client_mod, "litellm_request", litellm.request),
    ]
    for target_module, name, fake_impl in quota_fakes:
        monkeypatch.setattr(target_module, name, fake_impl)
    backend = make_fake_pool_backend()
    backend.add_paid_email(_USER_STUB_EMAIL)
    backend.install_on_app_module(app_mod, monkeypatch)
    return TestClient(web_app, raise_server_exceptions=False), entitlements_store, litellm


def _make_test_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a TestClient with the standard fakes (see ``_make_quota_test_client``)."""
    client, _entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    return client


_PLAN_VALUES_BY_NAME = {"free": FREE_PLAN_VALUES, "explorer": EXPLORER_PLAN_VALUES, "ally": ALLY_PLAN_VALUES}


def _seed_entitlements_row(
    entitlements_store: InMemoryEntitlementsStore,
    plan_name: str = "explorer",
    user_id: str = _USER_STUB_USER_ID,
    user_id_prefix: str = _USER_STUB_USER_ID_PREFIX,
    **overrides: float,
) -> None:
    """Insert an entitlements row copied from the named launch plan, with per-test quota overrides."""
    entitlements_store.insert_entitlements_if_absent(
        {
            "user_id": user_id,
            "user_id_prefix": user_id_prefix,
            "plan_name": plan_name,
            **{**_PLAN_VALUES_BY_NAME[plan_name], **overrides},
        }
    )


class _FakeLoginMethod:
    """Stand-in for a SuperTokens LoginMethod -- only ``email`` and ``verified`` are used."""

    def __init__(self, email: str | None, verified: bool = True) -> None:
        self.email = email
        self.verified = verified


def _make_pool_quota_test_client(
    monkeypatch: pytest.MonkeyPatch,
    pool_backend: FakePoolBackend | None = None,
) -> tuple[TestClient, FakePoolBackend, InMemoryEntitlementsStore, FakeLiteLLMBackend]:
    """Create a TestClient with pool-backend and quota fakes installed.

    The returned pool backend is seeded with the stub user email as paid, so
    the stub's lazily-created entitlements row resolves to the ally plan by
    default; free-plan tests flip the entry via ``backend.add_paid_email``
    (the lazy backfill never assigns explorer), and tests wanting any other
    plan write a row into the entitlements store directly.
    """
    client, entitlements_store, litellm = _make_quota_test_client(monkeypatch)
    monkeypatch.setenv("POOL_SSH_PRIVATE_KEY", "fake-management-key-pem")
    backend = pool_backend if pool_backend is not None else make_fake_pool_backend()
    backend.add_paid_email(_USER_STUB_EMAIL)
    backend.install_on_app_module(app_mod, monkeypatch)
    return client, backend, entitlements_store, litellm


def _make_pool_test_client(
    monkeypatch: pytest.MonkeyPatch,
    pool_backend: FakePoolBackend | None = None,
) -> tuple[TestClient, FakePoolBackend]:
    """Pool test client without the quota handles (see ``_make_pool_quota_test_client``)."""
    client, backend, _entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch, pool_backend)
    return client, backend


def _make_pool_quota_web_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakePoolBackend, InMemoryEntitlementsStore, FakeLiteLLMBackend, FakeSuperTokensBackend]:
    """Pool/quota client plus the browser-session seams, for web-chrome auth tests.

    On top of ``_make_pool_quota_test_client``, installs the SuperTokens fake
    so a test can establish a cookie-based browser session (sign up via
    ``st_backend``, then ``client.cookies.set(FakeSuperTokensBackend.
    BROWSER_SESSION_COOKIE, session.access_token)``) and exercise the
    resource endpoints without a Bearer header.
    """
    client, backend, entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    st_backend = make_fake_supertokens_backend()
    # install adopts the quota client's entitlements store, so both seams
    # resolve one store.
    st_backend.install_on_app_module(app_mod, monkeypatch)
    return client, backend, entitlements_store, litellm, st_backend


def _sign_in_browser_user(client: TestClient, st_backend: FakeSuperTokensBackend, email: str) -> str:
    """Sign up an email/password user (unverified) and plant its cookie-based browser session on ``client``.

    Returns the new user's SuperTokens user id; call
    ``st_backend.mark_email_verified`` on it when the test needs a verified
    account.
    """
    signup = st_backend.sign_up(tenant_id="public", email=email, password="pw-123456")
    assert isinstance(signup, EPSignUpOkResult)
    session = st_backend.sdk_create_browser_session(None, signup.user.id)
    client.cookies.set(FakeSuperTokensBackend.BROWSER_SESSION_COOKIE, session.access_token)
    return signup.user.id


def _admin_key_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_KEY_TEST_VALUE}"}


def _make_suspension_admin_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    TestClient,
    FakePoolBackend,
    InMemoryEntitlementsStore,
    FakeLiteLLMBackend,
    FakeSuperTokensBackend,
    FakeCloudflareOps,
    InMemoryKeyStore,
]:
    """Test client wiring every backend the suspend/unsuspend fan-out touches.

    Pool backend (workspaces + shares SQL), entitlements store, LiteLLM admin
    API, SuperTokens (email resolution + sessions), Cloudflare ops, and the
    R2 key store -- plus the admin key env var.
    """
    client, backend, entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    fake_ctx = make_fake_cloudflare_ctx()
    key_store = make_fake_key_store()
    # Single-loop patching (same pattern as the other client helpers) so the
    # monkeypatch ratchet only counts one occurrence.
    suspension_fakes: list[tuple[object, str, object]] = [
        (cloudflare_mod, "get_cloudflare_ctx", lambda: fake_ctx),
        (r2_stores_mod, "get_key_store", lambda: key_store),
    ]
    for target_module, name, fake_impl in suspension_fakes:
        monkeypatch.setattr(target_module, name, fake_impl)
    st_backend = make_fake_supertokens_backend()
    st_backend.install_on_app_module(app_mod, monkeypatch)
    return client, backend, entitlements_store, litellm, st_backend, fake_ctx.fake, key_store


def _make_paid_crud_test_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakePoolBackend]:
    """Test client with the admin key configured and a fresh paid-list backend."""
    client, backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    return client, backend


def _make_bucket_quota_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeCloudflareOps, InMemoryKeyStore, InMemoryEntitlementsStore, InMemoryGrantStore]:
    """Create a TestClient with the R2 fakes installed (Cloudflare ops + key/grant stores + entitlements)."""
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    # Build our own fake ctx so the fake is typed as FakeCloudflareCtx (which
    # exposes ``.fake``); re-patching get_cloudflare_ctx overrides the one the quota
    # client installed.
    fake_ctx = make_fake_cloudflare_ctx()
    store = make_fake_key_store()
    grant_store = make_fake_grant_store()
    # Single-loop patching (same pattern as the Fake*Backend.install_on_app_module
    # helpers) so the monkeypatch ratchet only counts one occurrence.
    bucket_fakes: list[tuple[object, str, object]] = [
        (cloudflare_mod, "get_cloudflare_ctx", lambda: fake_ctx),
        (r2_stores_mod, "get_key_store", lambda: store),
        (r2_stores_mod, "get_grant_store", lambda: grant_store),
    ]
    for target_module, name, fake_impl in bucket_fakes:
        monkeypatch.setattr(target_module, name, fake_impl)
    return client, fake_ctx.fake, store, entitlements_store, grant_store


def _make_bucket_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeCloudflareOps, InMemoryKeyStore]:
    """Bucket test client without the entitlements/grant handles (see ``_make_bucket_quota_test_client``)."""
    client, fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    return client, fake, store


def _make_sync_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, InMemorySyncStore, dict[str, str]]:
    """Create a TestClient with the in-memory sync store installed.

    Returns a mutable ``caller`` holder whose ``user_id`` entry the stubbed
    token-decode reads on every request, so tests can switch the calling user
    without another patch (keeps the monkeypatch ratchet at one occurrence,
    mirroring the bucket-test helper's single-loop pattern).
    """
    client = _make_test_client(monkeypatch)
    store = make_fake_sync_store()
    caller = {"user_id": _USER_STUB_USER_ID}
    sync_fakes: list[tuple[object, str, object]] = [
        (sync_mod, "get_sync_store", lambda: store),
        (auth_mod, "get_user_id_from_access_token", lambda token: caller["user_id"]),
    ]
    for target_module, name, fake_impl in sync_fakes:
        monkeypatch.setattr(target_module, name, fake_impl)
    return client, store, caller


# What a current client's full-record push names: every updatable column plus
# the row key and CAS revision. Tests that exercise put_record directly pass
# this so the preserve-on-absent UPDATE behaves like a full-record write.
ALL_RECORD_FIELDS_SENT: frozenset[str] = frozenset(UPDATABLE_RECORD_COLUMNS) | {"agent_id", "revision"}


def _store_record(
    host_id: str = "host-aaa111",
    agent_id: str = "agent-1",
    display_name: str = "my-workspace",
    state: str = "active",
    encrypted_secrets: bytes | None = None,
    revision: int = 1,
    backup_bucket: str | None = None,
) -> dict[str, Any]:
    """A store-layer record dict (raw-bytes secrets), as the endpoints hand to put_record."""
    return {
        "host_id": host_id,
        "agent_id": agent_id,
        "backup_bucket": backup_bucket,
        "display_name": display_name,
        "color": None,
        "provider_kind": "docker",
        "hosting_device_id": "device-1",
        "device_label": "laptop",
        "state": state,
        "restored_from_host_id": None,
        "encrypted_secrets": encrypted_secrets,
        "revision": revision,
        "record_format": 1,
    }


# Shared coordinates for the self-hosted sharing tests (shares / certs / broker).
_SHARE_STUB_TOKEN = "share-user-stub-jwt"
_SHARE_STUB_USER_ID = "12345678-1234-5678-1234-567812345678"
_SHARE_STUB_USER_LABEL = "12345678123456781234567812345678"
_SHARE_STUB_EMAIL = "sharer@example.com"
_SHARE_STUB_HOST_ID = "host-" + "a" * 32
_OTHER_HOST_ID = "host-" + "b" * 32
_CONTENT_DOMAIN = "minds-test.example"
_FRPS_SECRET = "frps-plugin-secret-8d1c44"

# The seeded test relay fleet: one relay per region, mirroring what the
# provisioning flow registers in the relays table.
_RELAY_ID_US1 = "relay-" + "1" * 16
_RELAY_ID_US2 = "relay-" + "2" * 16
_RELAY_ENDPOINT_US1 = "relay-us1.infra.example.com:7000"
_RELAY_ENDPOINT_US2 = "relay-us2.infra.example.com:7000"


def _share_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_SHARE_STUB_TOKEN}"}


def _make_share_test_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakePoolBackend]:
    """TestClient with sharing env config, a stubbed SuperTokens user, and the in-memory DB."""

    def _stub_user_id_from_token(token: str) -> str:
        if token != _SHARE_STUB_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid token")
        return _SHARE_STUB_USER_ID

    # The share endpoints resolve identity through the shared web-identity
    # path, whose Bearer leg also calls authenticate_request; stub both.
    def _stub_authenticate_request(request: Any, check_database: bool = False) -> UserAuth:
        del check_database
        auth_header = request.headers.get("authorization", "")
        if auth_header != f"Bearer {_SHARE_STUB_TOKEN}":
            raise HTTPException(status_code=401, detail="Invalid token")
        return UserAuth(
            user_id_prefix=derive_user_id_prefix(_SHARE_STUB_USER_ID),
            email=_SHARE_STUB_EMAIL,
            is_email_verified=True,
        )

    return _make_share_test_client_with_fakes(
        monkeypatch,
        {
            "get_user_id_from_access_token": _stub_user_id_from_token,
            "authenticate_request": _stub_authenticate_request,
        },
    )


class InMemoryDeviceAuthCodeStore:
    """In-memory stand-in for the Neon device-auth-code table."""

    def __init__(self) -> None:
        self.rows_by_code_hash: dict[str, dict[str, Any]] = {}

    def insert_code(
        self,
        code_hash: str,
        user_id: str,
        code_challenge: str,
        redirect_uri: str,
        expires_at: datetime,
    ) -> None:
        self.rows_by_code_hash[code_hash] = {
            "user_id": user_id,
            "code_challenge": code_challenge,
            "redirect_uri": redirect_uri,
            "expires_at": expires_at,
            "consumed_at": None,
        }

    def consume_code(self, code_hash: str) -> dict[str, Any] | None:
        row = self.rows_by_code_hash.get(code_hash)
        if row is None or row["consumed_at"] is not None:
            return None
        if row["expires_at"] <= datetime.now(timezone.utc):
            return None
        row["consumed_at"] = datetime.now(timezone.utc)
        return {
            "user_id": row["user_id"],
            "code_challenge": row["code_challenge"],
            "redirect_uri": row["redirect_uri"],
        }


class InMemorySignupAttemptStore:
    """In-memory stand-in for the Neon signup_attempts table."""

    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []
        # When set, both store methods raise it (the DB-outage fail-open path).
        self.error_to_raise: Exception | None = None

    def count_recent_attempts(self, client_ip: str, subnet: str | None) -> tuple[int, int]:
        if self.error_to_raise is not None:
            raise self.error_to_raise
        now = datetime.now(timezone.utc)
        ip_hour_count = sum(
            1
            for attempt in self.attempts
            if attempt["client_ip"] == client_ip and (now - attempt["attempted_at"]) < timedelta(hours=1)
        )
        subnet_day_count = sum(
            1
            for attempt in self.attempts
            if subnet is not None
            and attempt["subnet"] == subnet
            and (now - attempt["attempted_at"]) < timedelta(days=1)
        )
        return ip_hour_count, subnet_day_count

    def record_attempt(
        self,
        client_ip: str | None,
        subnet: str | None,
        email: str,
        signup_method: str,
        verdict: str,
        outcome: str,
        reputation_json: str | None,
    ) -> None:
        if self.error_to_raise is not None:
            raise self.error_to_raise
        self.attempts.append(
            {
                "attempted_at": datetime.now(timezone.utc),
                "client_ip": client_ip,
                "subnet": subnet,
                "email": email,
                "signup_method": signup_method,
                "verdict": verdict,
                "outcome": outcome,
                "reputation_json": reputation_json,
            }
        )

    def seed_attempts(self, client_ip: str, subnet: str | None, count: int) -> None:
        """Plant ``count`` recent attempts so a test can trip the velocity caps."""
        for _ in range(count):
            self.record_attempt(
                client_ip=client_ip,
                subnet=subnet,
                email="seeded@example.com",
                signup_method="password",
                verdict="clean",
                outcome="allowed",
                reputation_json=None,
            )


class InMemoryIpReputationCache:
    """In-memory stand-in for the Neon ip_reputation_cache table."""

    def __init__(self) -> None:
        self.entries_by_ip: dict[str, tuple[datetime, signup_hardening_module.IpReputation]] = {}
        # When set, every cache method raises it (the cache-outage fail-open path).
        self.error_to_raise: Exception | None = None

    def get_fresh(self, client_ip: str, ttl_seconds: int) -> signup_hardening_module.IpReputation | None:
        if self.error_to_raise is not None:
            raise self.error_to_raise
        entry = self.entries_by_ip.get(client_ip)
        if entry is None:
            return None
        fetched_at, reputation = entry
        if (datetime.now(timezone.utc) - fetched_at) > timedelta(seconds=ttl_seconds):
            return None
        return reputation

    def store(self, client_ip: str, reputation: signup_hardening_module.IpReputation) -> None:
        if self.error_to_raise is not None:
            raise self.error_to_raise
        self.entries_by_ip[client_ip] = (datetime.now(timezone.utc), reputation)

    def count_lookups_in_last_day(self) -> int:
        if self.error_to_raise is not None:
            raise self.error_to_raise
        now = datetime.now(timezone.utc)
        return sum(1 for fetched_at, _ in self.entries_by_ip.values() if (now - fetched_at) < timedelta(days=1))


class FakeIpReputationProvider:
    """Configurable reputation provider: per-IP flags, a not-configured default, and error injection."""

    def __init__(self) -> None:
        self.reputation_by_ip: dict[str, signup_hardening_module.IpReputation] = {}
        self.fetch_count = 0
        # When set, every fetch raises this instead of answering (the
        # provider-outage fail-open path).
        self.error_to_raise: Exception | None = None
        # When False the provider acts unconfigured (no token on this tier).
        self.is_configured = True

    def fetch_reputation(self, client_ip: str) -> signup_hardening_module.IpReputation | None:
        self.fetch_count += 1
        if self.error_to_raise is not None:
            raise self.error_to_raise
        if not self.is_configured:
            return None
        return self.reputation_by_ip.get(client_ip, signup_hardening_module.IpReputation())


class FakeTorExitList:
    """Static in-memory Tor exit set (no network)."""

    def __init__(self) -> None:
        self.exit_ips: set[str] = set()

    def is_tor_exit(self, client_ip: str) -> bool:
        return client_ip in self.exit_ips


def encode_attribution_cookie(payload: object) -> str:
    """Encode a payload the way the marketing site writes the imbue_attribution cookie.

    Percent-encoded JSON, i.e. ``encodeURIComponent(JSON.stringify(payload))``
    per docs/attribution-cookie-contract.md.
    """
    return quote(json.dumps(payload), safe="")


class InMemoryAttributionStore:
    """In-memory stand-in for the Neon attribution tables.

    Set ``raise_on_insert`` to exercise the recorders' fail-open path (the
    real store's failures surface as psycopg2 errors).
    """

    def __init__(self) -> None:
        self.account_rows: list[dict[str, Any]] = []
        self.download_rows: list[dict[str, Any]] = []
        self.raise_on_insert: Exception | None = None

    def insert_account_attribution(
        self,
        *,
        user_id: str,
        email: str,
        visitor_id: str | None,
        first_touch: dict[str, str] | None,
        last_touch: dict[str, str] | None,
        signup_context: str,
        signup_method: str,
    ) -> None:
        if self.raise_on_insert is not None:
            raise self.raise_on_insert
        # Write-once, mirroring the real store's ON CONFLICT DO NOTHING.
        if any(row["user_id"] == user_id for row in self.account_rows):
            return
        self.account_rows.append(
            {
                "user_id": user_id,
                "email": email,
                "visitor_id": visitor_id,
                "first_touch": first_touch,
                "last_touch": last_touch,
                "signup_context": signup_context,
                "signup_method": signup_method,
            }
        )

    def insert_download_event(
        self,
        *,
        visitor_id: str | None,
        first_touch: dict[str, str] | None,
        last_touch: dict[str, str] | None,
        platform: str,
        user_agent: str | None,
    ) -> None:
        if self.raise_on_insert is not None:
            raise self.raise_on_insert
        self.download_rows.append(
            {
                "visitor_id": visitor_id,
                "first_touch": first_touch,
                "last_touch": last_touch,
                "platform": platform,
                "user_agent": user_agent,
            }
        )


def hold_stable_download_link(url: str | None) -> None:
    """Put ``url`` -- or "could not be read" -- in the connector's stable-download cache.

    ``GET /download`` resolves the stable channel manifest over the network, so
    every test runs with an entry held (see the autouse fixture) and none of
    them reach the live feed. Tests that care what the link resolves to hold
    their own; the parsing tests call ``_arm64_dmg_url_from``, which does not
    read this cache.
    """
    cache = _stable_download_cache()
    cache.clear()
    cache[hashkey()] = url


def clear_stable_download_link() -> None:
    """Drop the held link, so the next read reaches the live feed."""
    _stable_download_cache().clear()


def read_stable_download_link() -> str | None:
    """What the last resolution left in the cache; ``None`` is a read that failed.

    Reading the cache rather than calling the resolver is what tells a route
    that resolved from one that never asked: the call would fill an empty cache
    itself.
    """
    cache = _stable_download_cache()
    key = hashkey()
    assert key in cache, "nothing has resolved the stable download link"
    return cache[key]


def _stable_download_cache() -> MutableMapping[Any, Any]:
    # `cached` types its cache as optional because passing None disables it.
    cache = accounts_web_module.stable_mac_arm64_url.cache
    assert cache is not None, "the stable download resolver is not cached"
    return cache


def _make_accounts_web_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeSuperTokensBackend, InMemoryDeviceAuthCodeStore]:
    """Test client for the hosted accounts surface (browser session seams + in-memory code store).

    The client speaks https so Secure cookies round-trip. After a fake
    signin/signup, plant the browser session with
    ``client.cookies.set(FakeSuperTokensBackend.BROWSER_SESSION_COOKIE,
    st_backend.last_browser_session.access_token)``.
    """
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://fake-supertokens.example.com")
    # The identity pages and the OAuth start change behavior when a dedicated
    # accounts origin or chrome origin is configured (misdirected requests are
    # refused with 421), so clear any ambient values: tests only see the
    # origins they set themselves.
    monkeypatch.delenv("ACCOUNTS_BASE_URL", raising=False)
    monkeypatch.delenv("SHARE_CHROME_ORIGIN", raising=False)
    st_backend = make_fake_supertokens_backend()
    st_backend.install_on_app_module(app_mod, monkeypatch)
    return (
        TestClient(web_app, base_url="https://testserver", raise_server_exceptions=False),
        st_backend,
        st_backend.device_code_store,
    )


def _make_share_test_client_with_fakes(
    monkeypatch: pytest.MonkeyPatch,
    session_fakes: dict[str, object],
) -> tuple[TestClient, FakePoolBackend]:
    """Shared client setup; ``session_fakes`` supplies the token -> user resolution to install on the auth module."""
    monkeypatch.setenv("SUPERTOKENS_CONNECTION_URI", "https://fake-supertokens.example.com")
    monkeypatch.setenv("SHARE_CONTENT_DOMAIN", _CONTENT_DOMAIN)
    monkeypatch.setenv("FRPS_AUTH_SECRET", _FRPS_SECRET)
    # Disable the Ping allow-cache so the kill-switch tests observe state
    # changes on the very next heartbeat.
    monkeypatch.setenv("MINDS_FRPS_PING_CACHE_TTL_SECONDS", "0")
    for name, fake_impl in session_fakes.items():
        monkeypatch.setattr(auth_mod, name, fake_impl)
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    return TestClient(web_app, raise_server_exceptions=False), backend
