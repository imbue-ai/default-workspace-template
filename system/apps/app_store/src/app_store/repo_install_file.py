"""The install file for an app from a GitHub repository: what a chat hands the store once it has worked out how to
install and start the repository, and (later) what a repository can ship itself.

It carries the same fields a store entry does -- how to install it (``recipe``), how to start it (``launch``), and
what it costs (``resources``) -- plus the repository and the one commit everything was tried at. It is held to exactly
the rules the shipped catalog is (``catalog.py``: pinned versions, argv lists and never a shell string, programs run
from the app's own environment, ``{port}`` passed, 127.0.0.1 bound), and a recipe that installs from the repository
must install that same commit.
"""

from typing import Final
from typing import Literal

from pydantic import Field
from pydantic import model_validator

from app_store.catalog import CommitHash
from app_store.catalog import GITHUB_ROOT
from app_store.catalog import GitRecipe
from app_store.catalog import GithubRepository
from app_store.catalog import InstallRecipe
from app_store.catalog import LaunchCommand
from app_store.catalog import ResourceEstimate
from app_store.catalog import short_commit
from app_store.errors import CatalogEntryError
from imbue.imbue_common.frozen_model import FrozenModel

REPO_INSTALL_FILE_FORMAT: Final[int] = 1


class RepoInstallFile(FrozenModel):
    """How to install and start one commit of a GitHub repository as a store app."""

    format: Literal[1] = Field(description="The file's format; 1")
    title: str = Field(min_length=1, max_length=64, description="The name users see")
    description: str = Field(min_length=1, max_length=200, description="One line on what it is for")
    about: str = Field(default="", description="A short paragraph for its detail page; empty to use the description")
    author: str = Field(default="", description="Who makes it; empty to use the repository's owner")
    license: str = Field(default="Unknown", min_length=1, description="Its license, as an SPDX identifier if known")
    repository: GithubRepository = Field(description="The repository, https://github.com/<owner>/<name>")
    commit: CommitHash = Field(description="The full hash of the commit it was tried at and installs")
    recipe: InstallRecipe = Field(description="How it is installed")
    launch: LaunchCommand = Field(description="How it is started")
    resources: ResourceEstimate = Field(description="What it costs, as measured while it was tried out")
    basis: str = Field(
        default="", description="How it was worked out and measured, in plain words, shown beside the figures"
    )

    @model_validator(mode="after")
    def _recipe_installs_this_commit(self) -> "RepoInstallFile":
        if isinstance(self.recipe, GitRecipe):
            if self.recipe.repository != self.repository:
                raise CatalogEntryError(
                    f"the recipe installs from {self.recipe.repository}, not this file's repository {self.repository}"
                )
            if self.recipe.commit != self.commit:
                raise CatalogEntryError(
                    f"the recipe installs commit {self.recipe.commit}, not this file's commit {self.commit}"
                )
        return self

    @property
    def owner_and_name(self) -> str:
        return self.repository.removeprefix(GITHUB_ROOT)

    @property
    def commit_url(self) -> str:
        return f"{self.repository}/commit/{self.commit}"

    @property
    def tree_url(self) -> str:
        return f"{self.repository}/tree/{self.commit}"

    @property
    def short_commit(self) -> str:
        return short_commit(self.commit)
