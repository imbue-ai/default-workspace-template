from abc import ABC, abstractmethod

from imbue.imbue_common.mutable_model import MutableModel

from workspace_themes.data_types import ThemeCatalog


class ThemeCatalogLoaderInterface(MutableModel, ABC):
    """Reads the workspace's themes as they are on disk now."""

    @abstractmethod
    def load(self) -> ThemeCatalog:
        """The catalog of every theme folder, read again only when a file it is drawn from changed."""
