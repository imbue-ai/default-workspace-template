import subprocess
from pathlib import Path

from skills_catalog.primitives import SKILLS_DIR
from skills_catalog.primitives import SKILL_FILENAME

TEMPLATE_BRANCH: str = "template-upstream"


def run_git(repo_root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=repo_root, check=True, capture_output=True, text=True).stdout


def write_skill(repo_root: Path, name: str, description: str) -> Path:
    """Write a conforming SKILL.md for the named skill and answer its path."""
    skill_file = repo_root / SKILLS_DIR / name / SKILL_FILENAME
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nDo the thing.\n")
    return skill_file


def commit_all(repo_root: Path, message: str) -> str:
    """Commit everything in the checkout and answer the new commit's hash."""
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "--allow-empty", "-m", message)
    return run_git(repo_root, "rev-parse", "HEAD").strip()


def init_workspace_repo(repo_root: Path, template_skill_names: tuple[str, ...]) -> Path:
    """A checkout whose root commit is the template, holding the named skills, on branch main."""
    repo_root.mkdir(parents=True, exist_ok=True)
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.invalid")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("a workspace\n")
    for name in template_skill_names:
        write_skill(repo_root, name, f"The template's {name}.")
    commit_all(repo_root, "Initial template")
    return repo_root


def merge_template_update(repo_root: Path, skill_names: tuple[str, ...], message: str) -> None:
    """Add the named skills on an upstream line off the root commit, then merge it as update-self does."""
    root_commit = run_git(repo_root, "rev-list", "--max-parents=0", "HEAD").strip()
    run_git(repo_root, "checkout", "-q", "-B", TEMPLATE_BRANCH, root_commit)
    for name in skill_names:
        write_skill(repo_root, name, f"The template's newer {name}.")
    commit_all(repo_root, "Upstream template change")
    run_git(repo_root, "checkout", "-q", "main")
    run_git(repo_root, "merge", "-q", "--no-ff", "-m", message, TEMPLATE_BRANCH)
