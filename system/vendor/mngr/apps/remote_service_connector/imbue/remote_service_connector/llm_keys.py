"""LiteLLM virtual-key management endpoints (/keys/*)."""

import logging
import re

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.entitlements as entitlements_module
import imbue.remote_service_connector.litellm_client as litellm_client
import imbue.remote_service_connector.sync as sync_module
from imbue.remote_service_connector.errors import QuotaExceededError
from imbue.remote_service_connector.http_api import handle_endpoint_errors

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateKeyRequest(BaseModel):
    key_alias: str | None = Field(default=None, description="Optional human-readable alias for the key")
    max_budget: float | None = Field(default=None, description="Optional max budget in USD (no limit if unset)")
    budget_duration: str | None = Field(
        default=None, description="Optional budget reset duration (e.g. '1d', '1h', '1w', '1M')"
    )
    metadata: dict[str, str] | None = Field(
        default=None, description="Optional metadata (e.g. agent_id, host_id) for resource tracking"
    )


class CreateKeyResponse(BaseModel):
    key: str = Field(description="The generated LiteLLM virtual key")
    base_url: str = Field(description="The LiteLLM proxy base URL for ANTHROPIC_BASE_URL")


class KeyInfo(BaseModel):
    token: str = Field(description="Hashed key token identifier")
    key_alias: str | None = Field(default=None, description="Human-readable alias")
    key_name: str | None = Field(default=None, description="Key name")
    spend: float = Field(default=0.0, description="Total spend in USD")
    max_budget: float | None = Field(default=None, description="Max budget in USD")
    budget_duration: str | None = Field(default=None, description="Budget reset duration")
    user_id: str | None = Field(default=None, description="User ID the key belongs to")


class UpdateBudgetRequest(BaseModel):
    max_budget: float | None = Field(default=None, description="New max budget in USD (null to remove limit)")
    budget_duration: str | None = Field(default=None, description="New budget reset duration (null to remove)")


class DeleteKeyResponse(BaseModel):
    status: str = Field(description="Deletion status")


@router.post("/keys/create")
def create_litellm_key(request: Request, body: CreateKeyRequest) -> dict[str, object]:
    """Create a new LiteLLM virtual key for the authenticated user.

    Refused with a quota error when the account's monthly LLM budget is zero
    (e.g. the explorer plan -- pick 'subscription' as the AI provider
    instead). Otherwise the account's user-level LiteLLM budget is upserted
    before the key is minted, so aggregate spend across every key is capped
    at the account's monthly quota by the time any key exists. Per-key
    budgets remain entirely caller-controlled.
    """
    with handle_endpoint_errors():
        user, user_id = accounts_web_module.resolve_web_user_identity(request)
        entitlements = entitlements_module.resolve_entitlements_for_user(user_id, user)
        _require_llm_spend_budget(entitlements)
        litellm_client.upsert_litellm_user_budget(user_id, entitlements.monthly_llm_spend_usd)

        litellm_body: dict[str, object] = {"user_id": user_id}
        if body.key_alias is not None:
            litellm_body["key_alias"] = body.key_alias
        if body.max_budget is not None:
            litellm_body["max_budget"] = body.max_budget
        if body.budget_duration is not None:
            litellm_body["budget_duration"] = body.budget_duration
        if body.metadata is not None:
            litellm_body["metadata"] = body.metadata

        resp = litellm_client.litellm_request("POST", "/key/generate", json_body=litellm_body)
        data = resp.json()

        return CreateKeyResponse(
            key=data["key"],
            base_url=litellm_client.litellm_base_url_for_agents(),
        ).model_dump()


@router.get("/keys")
def list_litellm_keys(request: Request) -> list[dict[str, object]]:
    """List all LiteLLM virtual keys owned by the authenticated user."""
    with handle_endpoint_errors():
        user_id = accounts_web_module.resolve_web_user_identity(request)[1]

        keys_raw = litellm_client.list_litellm_user_key_entries(user_id)
        result: list[dict[str, object]] = []
        for entry in keys_raw:
            if not isinstance(entry, dict):
                # Defensive: if LiteLLM ever flips back to bare token strings,
                # surface what we have rather than 500ing.
                result.append(KeyInfo(token=str(entry)).model_dump())
                continue
            result.append(
                KeyInfo(
                    token=entry.get("token", ""),
                    key_alias=entry.get("key_alias"),
                    key_name=entry.get("key_name"),
                    spend=entry.get("spend", 0.0),
                    max_budget=entry.get("max_budget"),
                    budget_duration=entry.get("budget_duration"),
                    user_id=entry.get("user_id"),
                ).model_dump()
            )
        return result


@router.get("/keys/{key_id}")
def get_litellm_key_info(request: Request, key_id: str) -> dict[str, object]:
    """Get info (including spend and budget) for a specific LiteLLM key."""
    with handle_endpoint_errors():
        user_id = accounts_web_module.resolve_web_user_identity(request)[1]

        resp = litellm_client.litellm_request("GET", "/key/info", params={"key": key_id})
        data = resp.json()

        info = data.get("info", data)
        if info.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Key does not belong to this user")

        return KeyInfo(
            token=info.get("token", ""),
            key_alias=info.get("key_alias"),
            key_name=info.get("key_name"),
            spend=info.get("spend", 0.0),
            max_budget=info.get("max_budget"),
            budget_duration=info.get("budget_duration"),
            user_id=info.get("user_id"),
        ).model_dump()


@router.put("/keys/{key_id}/budget")
def update_litellm_key_budget(request: Request, key_id: str, body: UpdateBudgetRequest) -> dict[str, object]:
    """Update the budget for a LiteLLM key owned by the authenticated user."""
    with handle_endpoint_errors():
        user_id = accounts_web_module.resolve_web_user_identity(request)[1]

        # Verify ownership
        info_resp = litellm_client.litellm_request("GET", "/key/info", params={"key": key_id})
        info_data = info_resp.json()
        info = info_data.get("info", info_data)
        if info.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Key does not belong to this user")

        update_body: dict[str, object] = {"key": key_id}
        update_body["max_budget"] = body.max_budget
        if body.budget_duration is not None:
            update_body["budget_duration"] = body.budget_duration

        litellm_client.litellm_request("POST", "/key/update", json_body=update_body)

        return {"status": "updated"}


@router.delete("/keys/{key_id}")
def delete_litellm_key(request: Request, key_id: str) -> dict[str, object]:
    """Delete a LiteLLM key owned by the authenticated user."""
    with handle_endpoint_errors():
        user_id = accounts_web_module.resolve_web_user_identity(request)[1]

        # Verify ownership
        info_resp = litellm_client.litellm_request("GET", "/key/info", params={"key": key_id})
        info_data = info_resp.json()
        info = info_data.get("info", info_data)
        if info.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Key does not belong to this user")

        litellm_client.litellm_request("POST", "/key/delete", json_body={"keys": [key_id]})

        return DeleteKeyResponse(status="deleted").model_dump()


# Budget defaults for workspace-minted keys, matching the desktop mint page
# (apps/minds .../ai_keys.py): a rolling daily budget, not a lifetime cap.
_WORKSPACE_MINT_MAX_BUDGET_USD = 100.0
_WORKSPACE_MINT_BUDGET_DURATION = "1d"

# Workspace-record host ids look like ``host-<32 hex>`` (mirrors the sync
# module's validation shape; kept permissive on length for older ids).
_WORKSPACE_HOST_ID_RE = re.compile(r"^host-[0-9a-f]{8,64}$")
# Workspace ids are the workspace's system-services agent id.
_WORKSPACE_ID_RE = re.compile(r"^agent-[0-9a-f]{8,64}$")


class WorkspaceMintRequest(BaseModel):
    host_id: str | None = Field(
        default=None,
        description=(
            "The workspace's current machine (`host-<hex>`). Compat addressing from clients that "
            "predate workspace ids; new clients send workspace_id instead."
        ),
    )
    workspace_id: str | None = Field(
        default=None,
        description="The workspace's id (`agent-<hex>`, the sync-record key); preferred over host_id",
    )


def _require_llm_spend_budget(entitlements: entitlements_module.AccountEntitlements) -> None:
    """Refuse key minting outright for plans with a zero monthly LLM budget."""
    monthly_budget = entitlements.monthly_llm_spend_usd
    if monthly_budget <= 0:
        raise QuotaExceededError(
            entitlement="monthly_llm_spend_usd",
            limit=monthly_budget,
            current=0,
            message=(
                "This account's plan has no LLM spend budget, so imbue-cloud inference keys cannot be "
                "created. Select 'subscription' (or your own API key) as the AI provider instead."
            ),
        )


@router.post("/keys/workspace-mint")
def mint_workspace_key(request: Request, body: WorkspaceMintRequest) -> dict[str, object]:
    """Mint (or rotate) the LiteLLM key for one of the caller's workspaces.

    The hosted web chrome's twin of the desktop mint page: the key's alias and
    metadata carry the workspace id, fixed server-side, so keys are
    attributable without any editable input. Ownership is record existence --
    the caller must have an ACTIVE sync record for the workspace. The alias is
    deterministic and LiteLLM enforces unique aliases, so an existing key with
    this alias is deleted and re-minted in place ("get me working credentials
    now" semantics: previously issued credentials for this workspace stop
    working).
    """
    with handle_endpoint_errors():
        user, user_id = accounts_web_module.resolve_web_user_identity(request)
        workspace_id = body.workspace_id.strip().lower() if body.workspace_id is not None else None
        host_id = body.host_id.strip().lower() if body.host_id is not None else None
        if workspace_id is not None and not _WORKSPACE_ID_RE.match(workspace_id):
            raise HTTPException(status_code=400, detail="Invalid workspace id")
        if host_id is not None and not _WORKSPACE_HOST_ID_RE.match(host_id):
            raise HTTPException(status_code=400, detail="Invalid workspace host id")
        if workspace_id is None and host_id is None:
            raise HTTPException(status_code=400, detail="workspace_id (or the compat host_id) is required")
        entitlements = entitlements_module.resolve_entitlements_for_user(user_id, user)
        _require_llm_spend_budget(entitlements)

        # Ownership: the caller's replica must hold an ACTIVE record for this
        # workspace (association IS record existence, same rule as the desktop).
        records = sync_module.get_sync_store().list_records(user_id)
        matching = next(
            (
                record
                for record in records
                if record["state"] == "active"
                and (record["agent_id"] == workspace_id if workspace_id is not None else record["host_id"] == host_id)
            ),
            None,
        )
        if matching is None:
            raise HTTPException(status_code=403, detail="No active workspace record for this workspace")
        workspace_id = str(matching["agent_id"])

        litellm_client.upsert_litellm_user_budget(user_id, entitlements.monthly_llm_spend_usd)

        # Rotate-on-exists: delete any key already carrying this workspace's
        # deterministic alias before minting the fresh one. Keys minted before
        # workspace keying carried the machine's host id in the alias, so both
        # shapes are rotated away.
        alias = f"workspace-{workspace_id}"
        stale_aliases = {alias, f"workspace-{matching['host_id']}"}
        keys_raw = litellm_client.list_litellm_user_key_entries(user_id)
        stale_tokens = [
            str(entry.get("token"))
            for entry in keys_raw
            if isinstance(entry, dict) and entry.get("key_alias") in stale_aliases and entry.get("token")
        ]
        if stale_tokens:
            litellm_client.litellm_request("POST", "/key/delete", json_body={"keys": stale_tokens})

        resp = litellm_client.litellm_request(
            "POST",
            "/key/generate",
            json_body={
                "user_id": user_id,
                "key_alias": alias,
                "max_budget": _WORKSPACE_MINT_MAX_BUDGET_USD,
                "budget_duration": _WORKSPACE_MINT_BUDGET_DURATION,
                "metadata": {"workspace_id": workspace_id, "source": "web-chrome-mint"},
            },
        )
        data = resp.json()
        return CreateKeyResponse(
            key=data["key"],
            base_url=litellm_client.litellm_base_url_for_agents(),
        ).model_dump()
