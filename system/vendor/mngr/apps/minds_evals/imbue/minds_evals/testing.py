import subprocess
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds_evals import evidence_collection


class LocalGitRepo(FrozenModel):
    """A throwaway local git repo standing in for a remote (unit tests make no network requests)."""

    repo_dir: Path = Field(description="The repo's working directory, usable as a git remote url")
    commit_shas: tuple[str, ...] = Field(description="Every commit sha on 'main', oldest first")


def commit_readme_revision(repo_dir: Path, readme_content: str, message: str) -> str:
    """Rewrite README.md, commit it on the repo's current branch, and return the new commit's sha."""
    (repo_dir / "README.md").write_text(readme_content)
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    # Identity and signing are set per invocation so the commit does not depend
    # on the developer's global git config (a global commit.gpgsign would try to
    # sign these throwaway commits and fail).
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "-c",
            "user.email=test@test",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
    )
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def make_local_git_repo(parent_dir: Path, repo_name: str, commit_count: int) -> LocalGitRepo:
    """Build a repo on branch 'main' whose every commit rewrites README.md with its own index, so a
    checkout's content identifies which commit it is at."""
    repo_dir = parent_dir / repo_name
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    commit_shas = [
        commit_readme_revision(
            repo_dir, "{} revision {}\n".format(repo_name, commit_idx), "commit {}".format(commit_idx)
        )
        for commit_idx in range(commit_count)
    ]
    return LocalGitRepo(repo_dir=repo_dir, commit_shas=tuple(commit_shas))


def program_block(program: str, *registrations: tuple[str, str]) -> str:
    """One supervisord `[program:*]` block that forwards a port for each (name, url) it registers."""
    forwards = " && ".join(
        "python3 system/scripts/forward_port.py --url {} --name {}".format(url, name) for name, url in registrations
    )
    return '[program:{}]\ncommand=bash -c "{}"\n\n'.format(program, forwards)


# The workspace's own system/supervisord.conf, which before the first turn is still the pinned
# template's file verbatim. Only an app whose forward_port.py call sits in the config is visible
# through it.
TEMPLATE_SUPERVISORD_CONF: Final[str] = "".join(
    (
        program_block("system_interface", ("system_interface", "http://localhost:8000")),
        "[program:terminal]\ncommand=bash system/apps/terminal/run_ttyd.sh\n\n",
        program_block("browser", ("browser", "http://localhost:8200")),
        program_block("files", ("files", "http://localhost:8300")),
        "[program:owner-exec]\ncommand=bash system/services/owner_exec/run.sh\n\n",
    )
)
TEMPLATE_CONFIG_REGISTRATIONS: Final[frozenset[str]] = frozenset({"system_interface", "browser", "files"})
# The template apps only the registry half sees; the registry also marks `owner-exec` `internal`.
SCRIPT_REGISTERED_APPS: Final[frozenset[str]] = frozenset({"terminal", "owner-exec"})
TEMPLATE_PREEXISTING_APPS: Final[frozenset[str]] = TEMPLATE_CONFIG_REGISTRATIONS | SCRIPT_REGISTERED_APPS

# A workspace agent id in the shape the forward proxy routes on (`agent-<32 hex>`). Mixed digits
# rather than one repeated character, so a wrong slice of it can never accidentally match.
FAKE_WORKSPACE_AGENT_ID: Final[str] = "agent-" + "0123456789abcdef" * 2


def probe_sections(**named_bodies: str) -> str:
    """What a multi-section box probe prints: each body under its section marker, in order."""
    return "".join(
        "{}\n{}".format(evidence_collection.section_marker(name), body) for name, body in named_bodies.items()
    )


def workspace_state_output(
    registry: str,
    *,
    registry_status: str = evidence_collection.STATUS_PRESENT,
    services: str = "",
    supervisord: str = "",
    isolated_instances: str = "",
) -> str:
    """What one `workspace_state_command` run prints, as both the driver's pre-turn-1 snapshot and
    the evidence collector read it."""
    return probe_sections(
        repo_root="/home/user/workspace\n",
        registry_status=registry_status + "\n",
        registry=registry,
        services=services,
        supervisord=supervisord,
        isolated_instances=isolated_instances,
    )
