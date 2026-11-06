#!/usr/bin/env python3
"""Look a GitHub repository up for adding to the App Store: ``python3 look_up_repo.py <link>``.

Takes any link a person might paste -- the repository, a branch or tag (``/tree/<ref>``), a release
(``/releases/tag/<tag>``), a commit (``/commit/<sha>``), with or without ``.git`` -- and pins it to one full commit
hash. Then it reads what working out an install needs, at that commit: the repository's description, license, size
and whether it is archived; its newest stable release; its top-level files; and the text of the files that say how it
installs and runs (``KEY_FILES`` and the README). Everything is printed as one JSON object and kept, raw, under
``data/.tasks/repo-adds/<owner>-<name>-<commit>/`` in the workspace.

Uses GitHub's public API without a sign-in (60 requests an hour is plenty: a look-up takes about ten). A private or
missing repository answers 404, reported as ``"is_reachable": false``. Standard library only.
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Final

API_ROOT: Final[str] = "https://api.github.com"
RAW_ROOT: Final[str] = "https://raw.githubusercontent.com"
TIMEOUT_SECONDS: Final[float] = 30.0
# The files that say how a repository installs and runs, read in full when present (the README is cut short).
KEY_FILES: Final[tuple[str, ...]] = (
    ".imbue-store.toml",
    "pyproject.toml",
    "setup.py",
    "package.json",
    "Dockerfile",
    "docker-compose.yml",
    "compose.yaml",
    "requirements.txt",
)
MAX_FILE_CHARACTERS: Final[int] = 20_000
MAX_README_CHARACTERS: Final[int] = 12_000
# The workspace this skill lives in: .agents/skills/add-repo-to-store/scripts/ is four levels below it.
RECORDS_DIRECTORY: Final[Path] = (
    Path(__file__).absolute().parents[4] / "data" / ".tasks" / "repo-adds"
)

_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/(?:tree|commit|releases/tag)/(?P<ref>[^?#]+?))?/?(?:[?#].*)?$"
)


class LookUpError(Exception):
    """The link is not a GitHub repository link, or GitHub could not be reached."""


def parse_link(link: str) -> tuple[str, str, str]:
    """The owner, name and ref (empty for the default branch) a GitHub link names."""
    match = _LINK_PATTERN.match(link.strip())
    if match is None:
        raise LookUpError(
            f"{link!r} is not a link to a GitHub repository (https://github.com/<owner>/<name>)."
        )
    return (
        match.group("owner"),
        match.group("name"),
        (match.group("ref") or "").strip("/"),
    )


def get_json(url: str) -> object | None:
    """The JSON at ``url``; None for a 404."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "app-store"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise LookUpError(
            f"GitHub answered {e.code} for {url}: {e.read()[:300]!r}"
        ) from e
    except urllib.error.URLError as e:
        raise LookUpError(f"GitHub could not be reached ({e.reason}).") from e


def get_text(url: str, limit: int) -> str | None:
    """The text at ``url``, cut to ``limit`` characters; None for a 404."""
    request = urllib.request.Request(url, headers={"User-Agent": "app-store"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read(limit * 4).decode("utf-8", errors="replace")[:limit]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise LookUpError(f"GitHub answered {e.code} for {url}.") from e
    except urllib.error.URLError as e:
        raise LookUpError(f"GitHub could not be reached ({e.reason}).") from e


def tag_commit(owner: str, name: str, tag: str) -> str | None:
    """The commit a tag points at, following an annotated tag to its commit."""
    ref = get_json(
        f"{API_ROOT}/repos/{owner}/{name}/git/ref/tags/{urllib.parse.quote(tag, safe='')}"
    )
    if not isinstance(ref, dict):
        return None
    target = ref.get("object") or {}
    for _ in range(3):
        if target.get("type") == "commit":
            return str(target["sha"])
        if target.get("type") != "tag":
            return None
        tag_object = get_json(str(target["url"]))
        if not isinstance(tag_object, dict):
            return None
        target = tag_object.get("object") or {}
    return None


def find_latest_release(owner: str, name: str) -> dict[str, object] | None:
    """The newest release that is not a pre-release or draft: its tag, the commit it points at, and when; None when
    the repository publishes no releases."""
    release = get_json(f"{API_ROOT}/repos/{owner}/{name}/releases/latest")
    if not isinstance(release, dict):
        return None
    tag = str(release["tag_name"])
    return {
        "tag": tag,
        "commit": tag_commit(owner, name, tag),
        "published_at": str(release.get("published_at") or ""),
        "url": str(release.get("html_url") or ""),
    }


_PYPROJECT_LICENSE: Final[re.Pattern[str]] = re.compile(
    r'^license\s*=\s*(?:"([^"]+)"|\{\s*text\s*=\s*"([^"]+)")', re.M
)


def license_from_files(files: dict[str, str]) -> str:
    """The license a project's own package file names, when GitHub could not tell; Unknown otherwise."""
    match = _PYPROJECT_LICENSE.search(files.get("pyproject.toml", ""))
    if match is not None:
        return (match.group(1) or match.group(2)).strip()
    try:
        package = json.loads(files.get("package.json", "") or "{}")
    except ValueError:
        package = {}
    if isinstance(package, dict) and isinstance(package.get("license"), str):
        return str(package["license"])
    return "Unknown"


def look_up(link: str) -> dict[str, object]:
    owner, name, ref = parse_link(link)
    repository = get_json(f"{API_ROOT}/repos/{owner}/{name}")
    if not isinstance(repository, dict):
        return {
            "is_reachable": False,
            "link": link,
            "what_happened": f"github.com/{owner}/{name} is private or doesn't exist; only public repositories can "
            "be added for now.",
        }
    owner, name = str(repository["owner"]["login"]), str(repository["name"])
    asked_ref = ref or str(repository["default_branch"])
    commit = get_json(
        f"{API_ROOT}/repos/{owner}/{name}/commits/{urllib.parse.quote(asked_ref, safe='')}"
    )
    if not isinstance(commit, dict):
        return {
            "is_reachable": False,
            "link": link,
            "what_happened": f"{asked_ref!r} isn't a branch, tag or commit of github.com/{owner}/{name}.",
        }
    sha = str(commit["sha"])
    listing = get_json(f"{API_ROOT}/repos/{owner}/{name}/contents/?ref={sha}")
    top_level = sorted(
        f"{entry['name']}/" if entry.get("type") == "dir" else str(entry["name"])
        for entry in (listing if isinstance(listing, list) else [])
    )
    files: dict[str, str] = {}
    for path in KEY_FILES:
        if path in top_level:
            text = get_text(
                f"{RAW_ROOT}/{owner}/{name}/{sha}/{path}", MAX_FILE_CHARACTERS
            )
            if text is not None:
                files[path] = text
    # The README, whatever its case or extension (readme.md, README.rst, ...).
    readme = next(
        (path for path in top_level if path.lower().split(".")[0] == "readme"), None
    )
    if readme is not None:
        text = get_text(
            f"{RAW_ROOT}/{owner}/{name}/{sha}/{readme}", MAX_README_CHARACTERS
        )
        if text is not None:
            files[readme] = text
    latest_release = find_latest_release(owner, name)
    license_info = repository.get("license") or {}
    license_id = (
        str(license_info.get("spdx_id") or "") if isinstance(license_info, dict) else ""
    )
    return {
        "is_reachable": True,
        "link": link,
        "repository": f"https://github.com/{owner}/{name}",
        "owner_and_name": f"{owner}/{name}",
        "asked_ref": asked_ref,
        "default_branch": str(repository["default_branch"]),
        "commit": sha,
        "commit_date": str(commit["commit"]["committer"]["date"]),
        "commit_url": f"https://github.com/{owner}/{name}/commit/{sha}",
        "description": repository.get("description") or "",
        "homepage": repository.get("homepage") or "",
        "license": license_id
        if license_id not in ("", "NOASSERTION")
        else license_from_files(files),
        "size_kb": repository.get("size"),
        "is_archived": bool(repository.get("archived")),
        "latest_release": latest_release,
        "language": repository.get("language") or "",
        "top_level": top_level,
        "files": files,
    }


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: look_up_repo.py <github link>\n")
        return 2
    try:
        found = look_up(sys.argv[1])
    except LookUpError as e:
        found = {"is_reachable": False, "link": sys.argv[1], "what_happened": str(e)}
    if found.get("is_reachable"):
        record = (
            RECORDS_DIRECTORY
            / f"{str(found['owner_and_name']).replace('/', '-')}-{str(found['commit'])[:12]}"
        )
        record.mkdir(parents=True, exist_ok=True)
        (record / "lookup.json").write_text(
            json.dumps(found, indent=2), encoding="utf-8"
        )
        found = {**found, "record_directory": str(record)}
    sys.stdout.write(json.dumps(found, indent=2) + "\n")
    return 0 if found.get("is_reachable") else 1


if __name__ == "__main__":
    sys.exit(main())
