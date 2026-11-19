"""The models of a local-model account: every model its server offers, kept in the two files that say so.

An account is signed in on one model, and its server usually has more. pi can switch a running
chat to any model its `models.json` names, and reads that file when it starts, so the file names
every model the server offers a chat here, at every context size on offer, from the start.
Beside it, `local_models.json` keeps what pi's file has no place for: which model each entry is
a size of, and whether it has passed the tool-call check.

Naming a model is not having it ready. On Ollama, the copy of a model at a context size is made
when a chat first asks for it, and no model but the one signed in on has been asked for a tool
call. Both happen as a chat switches (:func:`prepare_model_switch`), before pi hears of the
switch, so a model that cannot run a chat is refused with the reason while the chat stays where
it was.

The files are brought up to date with the server whenever a chat's model menu is opened
(:func:`refresh_account_models`): a model pulled since shows up, one removed goes. A chat that
was running before a model appeared reaches it once its agent has restarted. An account signed in
before `local_models.json` was kept gains it the same way, from what its `models.json` names.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from imbue.chat.harnesses.pi_coding.local_model import LOCAL_MODEL_PROVIDER_ID
from imbue.chat.harnesses.pi_coding.local_model import LocalModelError
from imbue.chat.harnesses.pi_coding.local_model import LocalModelProfile
from imbue.chat.harnesses.pi_coding.local_model import MIN_CONTEXT_WINDOW
from imbue.chat.harnesses.pi_coding.local_model import MODELS_FILENAME
from imbue.chat.harnesses.pi_coding.local_model import PLACEHOLDER_API_KEY
from imbue.chat.harnesses.pi_coding.local_model import list_models
from imbue.chat.harnesses.pi_coding.local_model import read_models_file_providers
from imbue.chat.harnesses.pi_coding.local_model import read_server_base_url
from imbue.chat.harnesses.pi_coding.local_model import verify_model
from imbue.chat.harnesses.pi_coding.local_model import write_models_file
from imbue.chat.harnesses.pi_coding.local_model_catalog import LocalModel
from imbue.chat.harnesses.pi_coding.local_model_catalog import LocalModelCatalogError
from imbue.chat.harnesses.pi_coding.local_model_catalog import context_model_id
from imbue.chat.harnesses.pi_coding.local_model_catalog import create_context_model
from imbue.chat.harnesses.pi_coding.local_model_catalog import ensure_context_model
from imbue.chat.harnesses.pi_coding.local_model_catalog import find_context_models
from imbue.chat.harnesses.pi_coding.local_model_catalog import group_model_choices
from imbue.chat.harnesses.pi_coding.local_model_catalog import read_ollama_models
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.utils.file_utils import atomic_write

ACCOUNT_MODELS_FILENAME: Final = "local_models.json"
_AUTH_FILENAME: Final = "auth.json"

# Ollama takes this effort to stop a model reasoning (measured on 0.40: every other leaves it on).
_OLLAMA_REASONING_OFF_EFFORT: Final = "none"


class LocalModelRecord(FrozenModel):
    """One model a local-model account's chats can run on, as pi is told of it."""

    # A file written by a newer build must stay readable after a downgrade.
    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="The model's id on the server, which is what pi runs")
    base_id: str = Field(description="The model this is a context size of; its own id on a server with no such copies")
    context_window: int = Field(description="The context size pi is told, in tokens")
    is_reasoning: bool = Field(default=False, description="Whether pi is told the model reasons before it answers")
    reasoning_off_effort: str | None = Field(
        default=None, description="The effort that stops the model reasoning, where the server is known to take one"
    )
    is_checked: bool = Field(default=False, description="Whether the model has answered the tool-call check")


class LocalModelAccount(FrozenModel):
    """The models of one local-model account. The first is the one a new chat starts on."""

    model_config = ConfigDict(extra="ignore")

    is_context_managed: bool = Field(
        description="Whether the server is Ollama, where a model's context size is a copy of it made on demand"
    )
    records: tuple[LocalModelRecord, ...] = Field(description="Every model on offer, at every context size on offer")


class LocalModelSignIn(FrozenModel):
    """What a sign-in on one model comes to before the model is checked: the id to check, and the account's models."""

    model_id: str = Field(description="The id of the model to check and start on, as the server runs it")
    context_window: int = Field(description="The context size to check it at, in tokens")
    account: LocalModelAccount = Field(description="Every model of the server, the one signed in on first")


@pure
def _to_profile(record: LocalModelRecord) -> LocalModelProfile:
    return LocalModelProfile(
        context_window=record.context_window,
        is_reasoning=record.is_reasoning,
        reasoning_off_effort=record.reasoning_off_effort,
    )


@pure
def _order_records(
    planned: Sequence[LocalModelRecord], known: Sequence[LocalModelRecord], first_id: str | None
) -> tuple[LocalModelRecord, ...]:
    """``planned`` with what is already known of each kept, in the known order, then the new ones."""
    known_by_id = {record.id: record for record in known}
    merged = [known_by_id.get(record.id, record) for record in planned]
    position_by_id = {record.id: position for position, record in enumerate(known)}
    is_not_first = [record.id != first_id for record in merged]
    order = sorted(
        range(len(merged)),
        key=lambda idx: (is_not_first[idx], position_by_id.get(merged[idx].id, len(known) + idx)),
    )
    return tuple(merged[idx] for idx in order)


@pure
def plan_ollama_records(models: Sequence[LocalModel]) -> tuple[LocalModelRecord, ...]:
    """A record for each model a chat can run on, at each context size it is offered at.

    A size that has a copy on the server is that copy; one that has none is named as the copy
    will be when it is made.
    """
    base_by_id = {model.id: model for model in models}
    records: list[LocalModelRecord] = []
    for choice in group_model_choices(models, MIN_CONTEXT_WINDOW):
        if choice.unusable_reason is not None:
            continue
        copies = find_context_models(models, choice.model_id)
        is_reasoning = base_by_id[choice.model_id].is_reasoning is True
        for context_window in choice.context_windows:
            records.append(
                LocalModelRecord(
                    id=copies.get(context_window, context_model_id(choice.model_id, context_window)),
                    base_id=choice.model_id,
                    context_window=context_window,
                    is_reasoning=is_reasoning,
                    reasoning_off_effort=_OLLAMA_REASONING_OFF_EFFORT if is_reasoning else None,
                )
            )
    return tuple(records)


@pure
def plan_listed_records(model_ids: Sequence[str], context_window: int) -> tuple[LocalModelRecord, ...]:
    """A record for each model of a server that says nothing of its models: each as it is listed, at one size."""
    return tuple(
        LocalModelRecord(id=model_id, base_id=model_id, context_window=context_window) for model_id in model_ids
    )


def plan_local_model_sign_in(
    client: httpx.Client, base_url: str, api_key: str, model_id: str, context_window: int
) -> LocalModelSignIn:
    """Work out which model of the server a sign-in on ``model_id`` runs, and the models the account gets.

    On Ollama ``model_id`` is a model and ``context_window`` one of its sizes: the copy at that
    size is found or made, and is what gets checked. Any other server runs ``model_id`` as named.
    Raises LocalModelError, worded for the user.
    """
    if model_id.strip() == "":
        raise LocalModelError("Pick a model.")
    described = read_ollama_models(client, base_url, api_key)
    if described is None:
        listed = list_models(client, base_url, api_key)
        other_ids = tuple(each for each in listed if each != model_id)
        records = plan_listed_records((model_id, *other_ids), context_window)
        return LocalModelSignIn(
            model_id=model_id,
            context_window=context_window,
            account=LocalModelAccount(is_context_managed=False, records=records),
        )
    choice = next(
        (each for each in group_model_choices(described, MIN_CONTEXT_WINDOW) if each.model_id == model_id), None
    )
    if choice is None:
        raise LocalModelError(f"The model server does not list {model_id}.")
    if choice.unusable_reason is not None:
        raise LocalModelError(f"{model_id} cannot run a chat here. {choice.unusable_reason}")
    # The largest size on offer that is no more than was asked for: a model is never named a
    # context longer than it takes.
    fitting = [size for size in choice.context_windows if size <= context_window]
    if not fitting:
        raise LocalModelError(
            f"A chat here needs a context window of at least {MIN_CONTEXT_WINDOW:,} tokens"
            " to hold the workspace's own instructions and a reply."
        )
    size = fitting[-1]
    try:
        run_id = ensure_context_model(client, base_url, api_key, described, model_id, size)
    except LocalModelCatalogError as e:
        raise LocalModelError(str(e)) from e
    records = _order_records(plan_ollama_records(described), (), run_id)
    return LocalModelSignIn(
        model_id=run_id,
        context_window=size,
        account=LocalModelAccount(is_context_managed=True, records=records),
    )


def plan_local_model_sign_in_with_own_client(
    base_url: str, api_key: str, model_id: str, context_window: int
) -> LocalModelSignIn:
    """:func:`plan_local_model_sign_in` over a client of its own, for the sign-in flow."""
    with httpx.Client() as client:
        return plan_local_model_sign_in(client, base_url, api_key, model_id, context_window)


@pure
def with_checked_model(account: LocalModelAccount, model_id: str, profile: LocalModelProfile) -> LocalModelAccount:
    """``account`` with ``model_id`` marked as having passed the check, told to pi as the check found it."""
    records = tuple(
        LocalModelRecord(
            id=record.id,
            base_id=record.base_id,
            context_window=profile.context_window,
            is_reasoning=profile.is_reasoning,
            reasoning_off_effort=profile.reasoning_off_effort,
            is_checked=True,
        )
        if record.id == model_id
        else record
        for record in account.records
    )
    return LocalModelAccount(is_context_managed=account.is_context_managed, records=records)


def write_account_models(account_dir: Path, base_url: str, account: LocalModelAccount) -> None:
    """Write the account's models: `models.json` for pi, and `local_models.json` beside it."""
    write_models_file(account_dir, base_url, {record.id: _to_profile(record) for record in account.records})
    atomic_write(account_dir / ACCOUNT_MODELS_FILENAME, account.model_dump_json(indent=2) + "\n")


def find_account_dir(config_dir: Path) -> Path:
    """The account folder behind a pi config dir: the dir itself, or where an agent's `models.json` link leads."""
    return (config_dir / MODELS_FILENAME).resolve().parent


def read_account_models(config_dir: Path) -> LocalModelAccount | None:
    """The models of the local-model account behind ``config_dir``; None for any other account.

    None too for an account signed in before this file was kept, which runs the one model its
    `models.json` names until it is signed in again.
    """
    path = find_account_dir(config_dir) / ACCOUNT_MODELS_FILENAME
    try:
        return LocalModelAccount.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValidationError) as e:
        logger.warning("Could not read the local models of {}: {}", path, e)
        return None


def _read_api_key(account_dir: Path) -> str:
    """The server's key as the account holds it: '' for a server that asks for none."""
    path = account_dir / _AUTH_FILENAME
    try:
        auth: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug("Could not read the model server key from {}: {}", path, e)
        return ""
    entry = auth.get(LOCAL_MODEL_PROVIDER_ID) if isinstance(auth, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    return key if isinstance(key, str) and key != PLACEHOLDER_API_KEY else ""


def _adopt_earlier_account(
    client: httpx.Client, account_dir: Path, base_url: str, api_key: str
) -> LocalModelAccount | None:
    """The models of an account signed in before `local_models.json` was kept, read from what pi was told.

    Its sign-in checked every model its `models.json` names, so each is taken as checked. What
    each is a context size of comes from the server as it is now; a server that does not answer
    leaves the account to be adopted another time.
    """
    provider = read_models_file_providers(account_dir).get(LOCAL_MODEL_PROVIDER_ID)
    entries = provider.get("models") if isinstance(provider, dict) else None
    named = [
        entry
        for entry in entries or ()
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("contextWindow"), int)
    ]
    if not named:
        return None
    described = read_ollama_models(client, base_url, api_key)
    if described is None:
        try:
            list_models(client, base_url, api_key)
        except LocalModelError as e:
            logger.debug("Left the local-model account in {} to be adopted later: {}", account_dir, e)
            return None
    base_id_by_id = {model.id: model.base_id for model in described or ()}
    records = tuple(
        LocalModelRecord(
            id=entry["id"],
            base_id=base_id_by_id.get(entry["id"], entry["id"]),
            context_window=entry["contextWindow"],
            is_reasoning=entry.get("reasoning") is True,
            reasoning_off_effort=_read_reasoning_off_effort(entry),
            is_checked=True,
        )
        for entry in named
    )
    return LocalModelAccount(is_context_managed=described is not None, records=records)


def _read_reasoning_off_effort(pi_model: dict[str, Any]) -> str | None:
    level_map = pi_model.get("thinkingLevelMap")
    effort = level_map.get("off") if isinstance(level_map, dict) else None
    return effort if isinstance(effort, str) else None


def refresh_account_models(client: httpx.Client, config_dir: Path) -> LocalModelAccount | None:
    """Bring the account's models up to date with its server, and return them; None for any other account.

    A server that does not answer leaves the account as it is.
    """
    account = read_account_models(config_dir)
    account_dir = find_account_dir(config_dir)
    base_url = read_server_base_url(account_dir)
    if base_url is None:
        return account
    api_key = _read_api_key(account_dir)
    is_adopted = False
    if account is None:
        # CLEANUP: drop this adoption (and _adopt_earlier_account) once the Local model provider has
        # shipped in a release: only workspaces built from its development branches hold an account
        # signed in before local_models.json was kept.
        account = _adopt_earlier_account(client, account_dir, base_url, api_key)
        is_adopted = account is not None
    if account is None or not account.records:
        return account
    first_id = account.records[0].id
    if account.is_context_managed:
        described = read_ollama_models(client, base_url, api_key)
        if described is None:
            return account
        planned = plan_ollama_records(described)
    else:
        try:
            listed = list_models(client, base_url, api_key)
        except LocalModelError as e:
            logger.debug("Left the local models of {} as they are: {}", account_dir, e)
            return account
        planned = plan_listed_records(listed, account.records[0].context_window)
    if not planned:
        return account
    refreshed = LocalModelAccount(
        is_context_managed=account.is_context_managed, records=_order_records(planned, account.records, first_id)
    )
    if refreshed != account or is_adopted:
        write_account_models(account_dir, base_url, refreshed)
    return refreshed


def refresh_account_models_with_own_client(config_dir: Path) -> LocalModelAccount | None:
    """:func:`refresh_account_models` over a client of its own, for a chat's model menu."""
    with httpx.Client() as client:
        return refresh_account_models(client, config_dir)


def prepare_model_switch(client: httpx.Client, config_dir: Path, model_id: str) -> None:
    """Make ``model_id`` ready for a chat of the account behind ``config_dir`` to switch to.

    Does nothing for a model the account already checked, and for any account that is not a
    local model's. Otherwise the model's copy at its context size is made where the server has
    none, and the model is asked for a tool call. Raises LocalModelError, worded for the user,
    when it cannot run a chat.
    """
    account = read_account_models(config_dir)
    record = next((each for each in account.records if each.id == model_id), None) if account is not None else None
    if account is None or record is None or record.is_checked:
        return
    account_dir = find_account_dir(config_dir)
    base_url = read_server_base_url(account_dir)
    if base_url is None:
        return
    api_key = _read_api_key(account_dir)
    if account.is_context_managed:
        described = read_ollama_models(client, base_url, api_key)
        if described is None:
            raise LocalModelError(f"The model server at {base_url} did not answer.")
        if not any(model.id == record.id for model in described):
            try:
                create_context_model(client, base_url, api_key, record.id, record.base_id, record.context_window)
            except LocalModelCatalogError as e:
                raise LocalModelError(str(e)) from e
    profile = verify_model(client, base_url, api_key, record.id, record.context_window)
    write_account_models(account_dir, base_url, with_checked_model(account, record.id, profile))


def prepare_model_switch_with_own_client(config_dir: Path, model_id: str) -> None:
    """:func:`prepare_model_switch` over a client of its own, for a chat's model switch."""
    with httpx.Client() as client:
        prepare_model_switch(client, config_dir, model_id)
