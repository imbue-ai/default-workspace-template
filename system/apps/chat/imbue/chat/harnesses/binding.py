"""Which account an agent runs under, and which harnesses a chat may change account on in place.

How a harness binds an agent to that account (the create's arguments, the rebind's edit) is
the harness's own ``AccountBinding`` (``account_binding``), registered on its ``HarnessSpec``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from imbue.chat import accounts
from imbue.chat.accounts import Account
from imbue.chat.accounts import choose_default_account
from imbue.chat.accounts import harness_for
from imbue.chat.harnesses.account_binding import BindingError
from imbue.chat.harnesses.harness_type import HarnessType


def resolve_binding(account_id: str = "", home: Path | None = None) -> Account:
    """The account a new agent should run under.

    An explicit id wins; otherwise the account the user pinned as the default; otherwise the
    most recently used account, which is bumped on every launch -- so signing in and then
    starting a chat "just works" without the caller having to name what it just created,
    while a pinned default keeps every unnamed launch on the harness the user chose. That
    rule is `accounts.choose_default_account`, shared with the workspace's create defaults.

    The account decides the harness (see `harness_for`), not the other way round: asking the
    caller for both invites a chat that names codex while running on an agy credential, and
    there is no way to notice that until its first turn fails.

    Raises when there are none. There is no shared login to fall back to (`~/.claude` is
    left alone), so an agent created without an account is simply unauthenticated. The
    instances API makes that unreachable (with nothing signed in it mints a chat that waits
    for an account, whose page offers the chooser), and this is the backstop for anything
    that does not.
    """
    if account_id:
        account = accounts.resolve_account(account_id, home)
        if harness_for(account) is None:
            raise BindingError(f"account {account_id} is on a lane this build does not have")
        return account

    chosen = choose_default_account(accounts.read_index(home))
    if chosen is None:
        raise accounts.AccountError("no provider accounts exist yet")
    # Back through `resolve_account` for the folder check. The explicit-id path above has
    # always had it; this one did not, so a row whose folder had gone bound an agent to a
    # directory that is not there -- which surfaces as an empty model bar, not as an error.
    return accounts.resolve_account(chosen.id, home)


def has_usable_account(home: Path | None = None) -> bool:
    """Whether any signed-in account is on a lane this build runs: what ``resolve_binding("")`` needs."""
    return any(harness_for(account) is not None for account in accounts.read_index(home).accounts)


# The harnesses a chat may change account on in place (a rebind, spec 6): every one with an
# account binding. A same-harness target on a harness outside this set takes the handoff path,
# which is the fallback the spec names for a harness that cannot resume under a swapped
# credential; drop a harness here to route it that way.
REBIND_VERIFIED_HARNESSES: Final[frozenset[HarnessType]] = frozenset(
    {HarnessType.CLAUDE, HarnessType.CODEX, HarnessType.PI_CODING, HarnessType.ANTIGRAVITY}
)


def is_rebind_supported(harness: HarnessType) -> bool:
    return harness in REBIND_VERIFIED_HARNESSES
