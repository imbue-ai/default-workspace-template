"""antigravity's session: the file session, plus the unknown-model rendering fallback.

Everything about sending, stopping and tapping is the shared file behaviour; the ONLY reason
this subclass exists is the model chip. See :func:`AntigravityHarnessSession.switch_options`.
"""

import threading

from imbue.system_interface.harnesses.antigravity.model import derived_option
from imbue.system_interface.harnesses.model import ModelOption
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import read_model_identity
from imbue.system_interface.harnesses.sending_registry import SendingRegistry
from imbue.system_interface.harnesses.session import FileHarnessSession
from imbue.system_interface.harnesses.session import SessionDeps


class AntigravityHarnessSession(FileHarnessSession):
    """agy's file session, with a derived option appended for a model the catalog lacks."""

    @classmethod
    def build(cls, deps: SessionDeps) -> "AntigravityHarnessSession":
        # Declared so the subclass is the STATIC type too; the base already constructs via
        # ``cls``, so this only narrows the annotation (same as CodexHarnessSession).
        self = cls.__new__(cls)
        self._deps = deps
        self._sending = SendingRegistry.build()
        self._sending_lock = threading.Lock()
        return self

    def switch_options(self) -> tuple[ModelOption, ...]:
        """The static catalog, plus a derived option when the LIVE model is not in it.

        This is where the catalog's staleness is absorbed. ``match_option`` resolves the
        reported id against this set, and an id it cannot find renders as the unrecognized
        shrug -- which is what a user sees for EVERY agy agent the moment Google ships a
        model newer than the hand-written list, including (worst case) a new default. Adding
        the derived option keeps the chip readable until the list is updated.

        Cheap enough for the recompute path: one small JSON read, the same file
        ``_recompute_model_choice`` has already read to get the identity it is matching.
        """
        options = self._deps.catalog_options()
        identity = read_model_identity(self._deps.model_state_path)
        if identity is None or match_option(identity, options) is not None:
            return options
        return (*options, derived_option(identity.model_id))
