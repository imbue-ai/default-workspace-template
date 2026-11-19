"""pi's harness session: the file session, with the models an agent's own `models.json` adds."""

from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.pi_coding.model import PI_CONFIG_DIR_RELPATH
from imbue.chat.harnesses.pi_coding.model import read_custom_model_options
from imbue.chat.harnesses.session import FileHarnessSession


class PiHarnessSession(FileHarnessSession):
    """A pi agent's session, whose option set includes the models its account brings itself."""

    def switch_options(self) -> tuple[ModelOption, ...]:
        """The bundled catalog, plus the models in this agent's `models.json`.

        An account on a local model server runs a model no catalog lists, and an identity that
        matches no option renders as no model at all. The file is the account's, linked into
        the agent's config dir at binding, so it follows the agent across a rebind.

        Cheap enough for the recompute path: one small JSON read, absent for every other account.
        """
        custom = read_custom_model_options(self._deps.state_dir / PI_CONFIG_DIR_RELPATH)
        return (*self._deps.catalog_options(), *custom)
