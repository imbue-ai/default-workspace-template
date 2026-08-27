import json
import re
from pathlib import Path
from typing import Final

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from loguru import logger

from versioning.claude_p import ClaudeCLIError
from versioning.claude_p import claude_p_completion
from versioning.data_types import SummaryGenerationError
from versioning.data_types import VersionSummary

SUMMARY_MODEL: Final[str] = "claude-haiku-4-5"

_SUMMARY_SYSTEM_PROMPT: Final[str] = (
    "You write one-line version descriptions for a non-technical user's app history view. "
    "You are given the commit messages (and a diff excerpt) for one version of an app. "
    "Respond with ONLY a JSON object: "
    '{"title": "<what specifically changed, 4-9 plain words, e.g. \'Articles now show where their facts came from\'>", '
    '"description": "<one or two short sentences adding detail the title could not fit>"}. '
    "The title must let a reader tell this version apart from its neighbors: name the concrete thing that "
    "changed, never a vague label like 'Visual refresh' or 'Improvements'. "
    "Write for someone who has never heard of git, code, or files. Describe outcomes, not implementation. "
    "No jargon, no file names."
)

_MAX_DIFF_EXCERPT_CHARS: Final[int] = 4000

_JSON_OBJECT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\{.*\}", re.DOTALL)


@pure
def _build_summary_prompt(commit_messages: list[str], diff_excerpt: str) -> str:
    messages_block = "\n\n".join(commit_messages)
    return (
        "Commit messages for this version:\n\n"
        f"{messages_block}\n\n"
        "Diff excerpt (for context only):\n\n"
        f"{diff_excerpt[:_MAX_DIFF_EXCERPT_CHARS]}"
    )


@pure
def _parse_summary_response(sha: str, response_text: str) -> VersionSummary:
    """Raises SummaryGenerationError if the response has no readable JSON object."""
    match = _JSON_OBJECT_PATTERN.search(response_text)
    if match is None:
        raise SummaryGenerationError(f"No JSON object in summary response: {response_text[:200]}")
    try:
        decoded = json.loads(match.group(0))
    except ValueError as e:
        raise SummaryGenerationError(f"Unreadable JSON in summary response: {response_text[:200]}") from e
    title = decoded.get("title")
    description = decoded.get("description")
    if not isinstance(title, str) or not isinstance(description, str) or not title.strip():
        raise SummaryGenerationError(f"Summary response missing title/description: {response_text[:200]}")
    return VersionSummary(sha=sha, title=title.strip(), description=description.strip())


def _summary_cache_file(cache_dir: Path, sha: str) -> Path:
    return cache_dir / f"{sha}.json"


def read_cached_summary(cache_dir: Path, sha: str) -> VersionSummary | None:
    cache_file = _summary_cache_file(cache_dir, sha)
    if not cache_file.exists():
        return None
    try:
        return VersionSummary.model_validate_json(cache_file.read_text())
    except ValueError:
        logger.warning("Discarding unreadable summary cache file {}", cache_file)
        return None


def generate_and_cache_summary(
    cache_dir: Path,
    sha: str,
    # The Versioning-Request trailer, used verbatim as the title when present.
    request_title: str | None,
    commit_message: str,
    diff_excerpt: str,
) -> VersionSummary:
    """Raises SummaryGenerationError if the model call fails or its response cannot be read."""
    cached = read_cached_summary(cache_dir, sha)
    if cached is not None:
        return cached
    try:
        result = claude_p_completion(
            _build_summary_prompt([commit_message], diff_excerpt),
            system=_SUMMARY_SYSTEM_PROMPT,
            model=SUMMARY_MODEL,
        )
    except (ClaudeCLIError, OSError) as e:
        raise SummaryGenerationError(f"Summary model call failed: {e}") from e
    generated = _parse_summary_response(sha, result.text)
    summary = (
        generated.model_copy_update(to_update(generated.field_ref().title, request_title))
        if request_title is not None
        else generated
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    _summary_cache_file(cache_dir, sha).write_text(summary.model_dump_json())
    return summary
