"""Orientation for a visiting agent, rendered at request time from the template this workspace runs.

The primer is the prose in ``.agents/shared/references/visiting-agents.md``
with the generated parts filled in (the skills index, the apps, the connected
accounts, recent changes); the skills index and the changes feed are also
served on their own so the helper can diff them. The instruction block an
inviter copies is ``visiting-agents-invite.md`` with the invite's values
substituted. Everything is computed from the checkout and its git history, so
nothing here can lag the workspace.
"""

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from share_gateway.agent_grants import AGENT_ACCESS_STATE_DIR
from share_gateway.agent_grants import WORKSPACE_SCOPE
from share_gateway.caddyfile import RegisteredApp
from share_gateway.log import log

SKILLS_DIR = Path(".agents/skills")
PRIMER_TEMPLATE_PATH = Path(".agents/shared/references/visiting-agents.md")
INVITE_TEMPLATE_PATH = Path(".agents/shared/references/visiting-agents-invite.md")
HELPER_REPO_PATH = Path("system/services/share_gateway/src/share_gateway/helper/wsvisit.cjs")
HELPER_PATH = Path(__file__).parent / "helper" / HELPER_REPO_PATH.name
HELPER_SOURCE_BASE_URL = "https://raw.githubusercontent.com/imbue-ai/default-workspace-template"
SKILLS_INDEX_CACHE_PATH = AGENT_ACCESS_STATE_DIR / "skills_index.json"
DEFAULT_CHANGES_WINDOW = timedelta(days=7)
_GIT_TIMEOUT_SECONDS = 30
_LATCHKEY_TIMEOUT_SECONDS = 20
_LATCHKEY_CACHE_SECONDS = 60.0
_MAX_CHANGES = 200
_EXEC_ONLY_BLOCK = re.compile(r"<!-- exec-only:start -->.*?<!-- exec-only:end -->\n?", re.DOTALL)
_EXEC_ONLY_MARKER = re.compile(r"<!-- exec-only:(?:start|end) -->\n?")
_FRONTMATTER_FIELD = re.compile(r"^(name|description):\s*(.*)$")


def _run_git(repo_root: Path, args: Sequence[str]) -> str:
    """git's stdout, or '' when git fails (an orientation route never fails on git, but the failure is logged)."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"git {' '.join(args)} in {repo_root} did not run: {exc}")
        return ""
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-300:]
        log(f"git {' '.join(args)} in {repo_root} exited {result.returncode}: {detail}")
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def _read_frontmatter(skill_file: Path) -> dict[str, str]:
    """``name`` and ``description`` from a SKILL.md's leading YAML block (the only two fields the index needs)."""
    fields: dict[str, str] = {}
    try:
        lines = skill_file.read_text().splitlines()
    except OSError as exc:
        log(f"Could not read {skill_file} for the skills index: {exc}")
        return fields
    if not lines or lines[0].strip() != "---":
        return fields
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = _FRONTMATTER_FIELD.match(line)
        if match:
            value = match.group(2).strip()
            # A quoted YAML scalar is unquoted; a folded or multi-line one keeps its first line.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            fields[match.group(1)] = value
    return fields


def _skill_content_hash(repo_root: Path, skill_dir: Path) -> str:
    listed = _run_git(repo_root, ["ls-files", "-z", "--", str(skill_dir)])
    digest = hashlib.sha256()
    for relative in sorted(entry for entry in listed.split("\0") if entry):
        digest.update(relative.encode("utf-8") + b"\0")
        try:
            digest.update((repo_root / relative).read_bytes())
        except OSError as exc:
            log(f"Could not read {relative} for the skills index hash: {exc}")
            continue
        digest.update(b"\0")
    return digest.hexdigest()


def _skill_last_changed_at(repo_root: Path, skill_dir: Path) -> str:
    return _run_git(repo_root, ["log", "-1", "--format=%cI", "--", str(skill_dir)]).strip()


def build_skills_index(repo_root: Path, template_ref: str) -> dict[str, object]:
    """The index of every skill: name, description, path, a content hash, and when it last changed."""
    skills: list[dict[str, object]] = []
    skills_root = repo_root / SKILLS_DIR
    if skills_root.is_dir():
        for skill_dir in sorted(entry for entry in skills_root.iterdir() if entry.is_dir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            relative_dir = SKILLS_DIR / skill_dir.name
            frontmatter = _read_frontmatter(skill_file)
            skills.append(
                {
                    "name": frontmatter.get("name", skill_dir.name),
                    "description": frontmatter.get("description", ""),
                    "path": str(relative_dir / "SKILL.md"),
                    "sha256": _skill_content_hash(repo_root, relative_dir),
                    "last_changed_at": _skill_last_changed_at(repo_root, relative_dir),
                }
            )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "template_ref": template_ref,
        "skills": skills,
    }


def cached_skills_index(repo_root: Path, cache_path: Path, template_ref: str) -> dict[str, object]:
    """The index for the current HEAD, computed once per commit and kept under ``data/.state``."""
    head = _run_git(repo_root, ["rev-parse", "HEAD"]).strip()
    if head:
        try:
            cached = json.loads(cache_path.read_text())
        except FileNotFoundError:
            cached = None
        except (OSError, ValueError) as exc:
            log(f"Could not read the skills index cache at {cache_path}; recomputing it: {exc}")
            cached = None
        if isinstance(cached, dict) and cached.get("head") == head and isinstance(cached.get("index"), dict):
            return cached["index"]
    index = build_skills_index(repo_root, template_ref)
    if head:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"head": head, "index": index}))
        except OSError as exc:
            log(f"Could not cache the skills index at {cache_path}; it will be recomputed on every request: {exc}")
    return index


def build_changes_feed(repo_root: Path, since: datetime) -> dict[str, object]:
    """What changed since ``since``: commits (with their top-level paths), the template updates among them, the skills touched."""
    raw = _run_git(
        repo_root,
        [
            "log",
            f"--since={since.isoformat()}",
            f"--max-count={_MAX_CHANGES}",
            "--format=%x1e%H%x1f%cI%x1f%an%x1f%s",
            "--name-only",
        ],
    )
    commits: list[dict[str, object]] = []
    template_updates: list[dict[str, object]] = []
    touched_skills: set[str] = set()
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        header, _, body = record.partition("\n")
        parts = header.split("\x1f")
        if len(parts) != 4:
            continue
        commit_hash, committed_at, author, subject = parts
        paths = [line.strip() for line in body.splitlines() if line.strip()]
        top_level_paths = sorted({path.split("/", 1)[0] for path in paths})
        for path in paths:
            if path.startswith(f"{SKILLS_DIR}/"):
                touched_skills.add(path[len(f"{SKILLS_DIR}/") :].split("/", 1)[0])
        entry: dict[str, object] = {
            "commit": commit_hash,
            "committed_at": committed_at,
            "author": author,
            "subject": subject,
            "paths": top_level_paths,
        }
        commits.append(entry)
        if subject.startswith("update-self:"):
            template_updates.append(entry)
    return {
        "since": since.isoformat(),
        "commits": commits,
        "template_updates": template_updates,
        "skills_changed": sorted(touched_skills),
    }


_INITIAL_WORKSPACE_COMMIT_SUBJECT = "Initial workspace commit"
_UPDATE_SELF_SUBJECT_PREFIX = "update-self:"


def template_commit(repo_root: Path) -> str:
    """The upstream template commit this workspace runs: the one it was created from or last updated to.

    A workspace's HEAD is never a template commit: bootstrap writes an ``Initial workspace commit`` on top of the
    cloned template, and later work sits on that. The newest marker on the first-parent history says where the
    template is (the markers ``.agents/shared/scripts/resolve_template_base.py`` reads): an ``update-self:`` merge
    names the upstream commit as its second parent, and the initial commit's first parent is the template it was
    written on (the initial commit itself when it has no parent). HEAD when the history has no marker; '' when git
    cannot read it.
    """
    lines = _run_git(repo_root, ["log", "--first-parent", "--format=%H%x09%P%x09%s", "HEAD"]).splitlines()
    for line in lines:
        sha, _, rest = line.partition("\t")
        parents, _, subject = rest.partition("\t")
        parent_shas = parents.split()
        if subject.startswith(_UPDATE_SELF_SUBJECT_PREFIX) and len(parent_shas) == 2:
            return parent_shas[1]
        if subject == _INITIAL_WORKSPACE_COMMIT_SUBJECT:
            return parent_shas[0] if parent_shas else sha
    return lines[0].split("\t", 1)[0] if lines else ""


def template_ref(repo_root: Path) -> str:
    """The template ref this workspace runs: the nearest tag when there is one, else the short commit."""
    commit = template_commit(repo_root)
    described = _run_git(repo_root, ["describe", "--tags", "--always", commit]).strip() if commit else ""
    return described or "unknown"


def template_source_ref(repo_root: Path) -> str:
    """A ref the template repository serves this workspace's files at: the tag when the template commit is tagged,
    else that full commit. ``main`` when git cannot say, which is logged, since the helper fetched then is not
    pinned."""
    commit = template_commit(repo_root)
    if not commit:
        log(f"Could not resolve the template commit of {repo_root}; the helper's source points at main")
        return "main"
    tags = _run_git(repo_root, ["tag", "--points-at", commit, "--sort=-v:refname"]).split()
    return tags[0] if tags else commit


def helper_source_url(repo_root: Path) -> str:
    """Where the helper this workspace runs is published: the template repository at the workspace's exact ref."""
    return f"{HELPER_SOURCE_BASE_URL}/{template_source_ref(repo_root)}/{HELPER_REPO_PATH.as_posix()}"


def file_sha256(path: Path) -> str:
    """The hex SHA-256 of a file, or '' when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        log(f"Could not hash {path}: {exc}")
        return ""


class LatchkeySummaryCache:
    """The connected-accounts summary, re-read at most once a minute (it shells out)."""

    def __init__(self) -> None:
        self._text = ""
        self._read_at = 0.0

    def read(self, environ: "os._Environ[str] | dict[str, str]", now_monotonic: float) -> str:
        if self._text and now_monotonic - self._read_at < _LATCHKEY_CACHE_SECONDS:
            return self._text
        self._text = summarize_connected_accounts(environ)
        self._read_at = now_monotonic
        return self._text


def _run_captured(argv: Sequence[str], environ: "os._Environ[str] | dict[str, str]") -> str | None:
    try:
        result = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_LATCHKEY_TIMEOUT_SECONDS,
            check=False,
            env=dict(environ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def summarize_connected_accounts(environ: "os._Environ[str] | dict[str, str]") -> str:
    """The latchkey services known here and the rules the host policy already grants, as Markdown; never raises."""
    gateway = environ.get("LATCHKEY_GATEWAY", "")
    if not gateway:
        return "The connected accounts could not be read: this process has no latchkey gateway configured."
    sections: list[str] = []
    services_text = _run_captured(["latchkey", "services", "list"], environ)
    if services_text is None:
        sections.append("The connected services could not be listed right now.")
    else:
        sections.append(
            "Services latchkey knows on this machine (`latchkey services list`):\n\n```\n"
            + services_text.strip()
            + "\n```"
        )
    rules_text = _run_captured(
        ["latchkey", "curl", "-s", "http://latchkey-self.invalid/permissions/self"],
        environ,
    )
    rules = _granted_rule_names(rules_text)
    if rules is None:
        sections.append("The permissions already granted could not be read right now.")
    elif rules:
        sections.append(
            "Permissions already granted to this workspace:\n\n" + "\n".join(f"- `{rule}`" for rule in rules)
        )
    else:
        sections.append("No third-party permissions are granted to this workspace yet.")
    return "\n\n".join(sections)


def _granted_rule_names(rules_text: str | None) -> list[str] | None:
    if rules_text is None:
        return None
    try:
        document = json.loads(rules_text)
    except ValueError:
        return None
    rules = document.get("rules") if isinstance(document, dict) else None
    if not isinstance(rules, list):
        return None
    names: list[str] = []
    for rule in rules:
        if isinstance(rule, dict):
            for scope, permissions in rule.items():
                if isinstance(permissions, list):
                    names.append(f"{scope}: {', '.join(str(permission) for permission in permissions)}")
                else:
                    names.append(str(scope))
    return names


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def render_skills_table(index: dict[str, object]) -> str:
    skills = index.get("skills")
    if not isinstance(skills, list) or not skills:
        return "This workspace has no skills yet."
    rows = [
        [
            str(skill.get("name", "")),
            str(skill.get("description", "")),
            str(skill.get("path", "")),
        ]
        for skill in skills
        if isinstance(skill, dict)
    ]
    return _markdown_table(("Skill", "What it covers", "Read"), rows)


def render_apps_table(
    apps: Sequence[RegisteredApp],
    workspace_domain: str,
    app_names: Sequence[str] | None,
) -> str:
    rows: list[list[str]] = []
    for app in sorted(apps, key=lambda entry: entry.name):
        if app_names is None and not app.is_grantable:
            continue
        if app_names is not None and app.name not in app_names:
            continue
        share_origin = f"https://{app.label}.{workspace_domain}"
        rows.append(
            [
                app.name,
                app.description or "(no description)",
                f"http://{app.backend_host}:{app.backend_port}",
                share_origin,
                f"{share_origin}{app.api_schema_path}" if app.api_schema_path else "(none)",
            ]
        )
    if not rows:
        return "No apps are registered right now."
    return _markdown_table(
        ("App", "What it is for", "Inside the workspace", "Over the share", "API description"), rows
    )


def render_changes_summary(feed: dict[str, object]) -> str:
    commits = feed.get("commits")
    if not isinstance(commits, list) or not commits:
        return "Nothing has changed in the window."
    lines: list[str] = []
    for entry in commits[:40]:
        if not isinstance(entry, dict):
            continue
        marker = "template update" if str(entry.get("subject", "")).startswith("update-self:") else "change"
        lines.append(f"- {entry.get('committed_at', '')} ({marker}): {entry.get('subject', '')}")
    skills_changed = feed.get("skills_changed")
    if isinstance(skills_changed, list) and skills_changed:
        lines.append("")
        lines.append("Skills touched: " + ", ".join(f"`{name}`" for name in skills_changed))
    return "\n".join(lines)


def _substitute(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _cut_to_scope(template: str, is_workspace_scope: bool) -> str:
    """The template without its exec-only passages for an app scope, and without their marker lines otherwise."""
    if is_workspace_scope:
        return _EXEC_ONLY_MARKER.sub("", template)
    return _EXEC_ONLY_BLOCK.sub("", template)


def render_primer(
    repo_root: Path,
    scope: "str | list[str]",
    workspace_display_name: str,
    workspace_id: str,
    skills_index: dict[str, object],
    apps: Sequence[RegisteredApp],
    workspace_domain: str,
    connected_accounts: str,
    changes: dict[str, object],
) -> str:
    """The primer for one agent: the reference prose, cut to the scope, with every generated part filled in."""
    try:
        template = (repo_root / PRIMER_TEMPLATE_PATH).read_text()
    except OSError as exc:
        log(f"Could not read the primer prose at {repo_root / PRIMER_TEMPLATE_PATH}; rendering a stub: {exc}")
        template = "# Visiting this workspace\n\nThe primer prose is missing from this workspace.\n\n{{skills_table}}\n\n{{apps_table}}\n"
    is_workspace_scope = scope == WORKSPACE_SCOPE
    template = _cut_to_scope(template, is_workspace_scope)
    app_names = None if is_workspace_scope else list(scope)
    scope_text = (
        "the whole workspace, with exec authority"
        if is_workspace_scope
        else "these apps only: " + ", ".join(app_names or [])
    )
    return _substitute(
        template,
        {
            "workspace_name": workspace_display_name or workspace_id or "this workspace",
            "workspace_id": workspace_id or "unknown",
            "scope": scope_text,
            "skills_table": render_skills_table(skills_index),
            "apps_table": render_apps_table(apps, workspace_domain, app_names),
            "connected_accounts": connected_accounts,
            "changes": render_changes_summary(changes),
            "template_ref": str(skills_index.get("template_ref", "unknown")),
        },
    )


def render_instruction_block(
    repo_root: Path,
    auth_origin: str,
    code: str,
    expires_at: datetime,
    scope: "str | list[str]",
    workspace_display_name: str,
    name_hint: str,
    app_origins: Sequence[dict[str, str]],
    helper_source_url: str,
    helper_sha256: str,
) -> str:
    """The block an inviter copies: the reference text with the invite's values, cut to the scope."""
    try:
        template = (repo_root / INVITE_TEMPLATE_PATH).read_text()
    except OSError as exc:
        log(f"Could not read the instruction block at {repo_root / INVITE_TEMPLATE_PATH}; rendering a stub: {exc}")
        template = (
            'You have been invited to the Imbue Studio workspace "{{workspace_name}}".\n'
            "Install the helper from {{helper_source_url}} (SHA-256 {{helper_sha256}}; the workspace also serves it "
            "at {{auth_origin}}/_agent/helper) and run: wsvisit join {{auth_origin}} {{code}}\n"
        )
    is_workspace_scope = scope == WORKSPACE_SCOPE
    template = _cut_to_scope(template, is_workspace_scope)
    apps_lines = (
        "\n".join(f"- {entry['name']}: {entry['origin']}" for entry in app_origins) or "- (none registered yet)"
    )
    return _substitute(
        template,
        {
            "workspace_name": workspace_display_name or "this workspace",
            "auth_origin": auth_origin,
            "code": code,
            "expires_at": expires_at.astimezone(timezone.utc).strftime("%H:%M UTC"),
            "name_placeholder": name_hint or "<a short name for yourself>",
            "scope": "the whole workspace" if is_workspace_scope else "these apps: " + ", ".join(scope),
            "apps": apps_lines,
            "helper_source_url": helper_source_url,
            "helper_sha256": helper_sha256,
        },
    )


def parse_since(raw: str) -> datetime | None:
    """A ``since`` argument as an aware datetime; None when empty, malformed, or naive."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def default_changes_since(now: datetime) -> datetime:
    return now - DEFAULT_CHANGES_WINDOW
