from app_instances.errors import AppInstancesError
from app_instances.errors import InstanceConflictError


class ChatInstancesError(AppInstancesError):
    """Base error of the chat app's instances source; the blueprint answers every subclass with a detail body."""


class ChatCreateRefusedError(ChatInstancesError, InstanceConflictError):
    """The chat app cannot start a chat right now: no provider is signed in, or mngr refused the create (a 409)."""


class ChatDestroyFailedError(ChatInstancesError):
    """``mngr destroy`` failed for a chat that exists (a 500 with mngr's own words)."""


class ChatRenameFailedError(ChatInstancesError):
    """``mngr rename`` failed for a chat that exists (a 500 with mngr's own words)."""
