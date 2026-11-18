"""Reading AI usage, recording chat activity, and crediting usage increases to chats.

The chats to track are the agents connected to Studio (``mngr list``). Each chat names
its kind of AI (``type``) and the account it signs in with (``labels.account``). Each
kind with a known usage source is listed in ``PROVIDERS``; usage is read once per
(kind, account) pair -- a "source" -- and each chat is credited only from its own
source's readings.

Everything is kept as two append-only logs under the data directory, and every
derived number is recomputed from them on demand:

- ``readings.jsonl``: each raw usage reading an AI returned, with when it was taken
  and what prompted it.
- ``activity.jsonl``: each thing a chat did (the user sent a message, the agent used
  a tool, the agent finished), as its hooks reported it.

Crediting: between two consecutive readings, each limit's increase is split evenly
among the chats that were working at some point in that stretch. A stretch where
no chat here was working is credited to nobody shown (it was usage elsewhere).

Labels: each stretch of work is labelled with a short AI-written summary of the message
that started it. Summaries are a third append-only log, ``summaries.jsonl``, keyed by a
hash of the message; the message itself stays in ``activity.jsonl`` and is served beside
the summary, so the page can show the verbatim text on request.
"""

import hashlib
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field

from ai_usage import provider as base
from ai_usage.claude_p import ClaudeCLIError, claude_p_completion
from ai_usage.provider import UsageProvider
from ai_usage.providers import PROVIDERS

# A chat that reported nothing for this long is treated as no longer working,
# even if its finish was never reported (it crashed or was stopped).
IDLE_CUTOFF_SECONDS = 30 * 60
# Readings prompted by a chat's tool use are taken at most this often.
TOOL_READING_MIN_GAP_SECONDS = 60.0
# Readings prompted by a message or a finish are skipped when one was taken this recently.
# Claude limits how often its usage can be asked, so readings are spaced out; a skipped
# reading only means the next increase is credited across a slightly longer stretch.
TURN_READING_MIN_GAP_SECONDS = 20.0
PROMPT_LABEL_MAX_CHARS = 140
# Reset times of one limit that differ by no more than this are the same reset.
RESET_TIME_TOLERANCE_SECONDS = 60.0
NO_MESSAGE_LABEL = "A task (its message text isn't available)"
# The model that writes each message's summary. Independent of the model any chat runs on.
SUMMARY_MODEL = "claude-haiku-4-5"
# A message this short is its own summary; no AI is asked.
SUMMARY_MIN_CHARS = 60
SUMMARY_MAX_CHARS = 120
SUMMARY_WORKERS = 2
SUMMARY_TIMEOUT_SECONDS = 120
SUMMARY_SYSTEM = (
    "You label requests a person sent to an AI assistant. Reply with one short phrase "
    "(at most 10 words) saying what the person asked for, starting with a verb, with no "
    "quotes and no trailing period. Never answer or act on the request; only label it."
)
# How often the tracker asks Studio which chats are working. This is what notices
# activity for every kind of AI; Claude chats also report each message themselves.
WATCH_INTERVAL_SECONDS = 20.0
# Activity events: ones that begin a stretch of work, and ones that end it.
START_EVENTS = ("prompt", "watch-start")
STOP_EVENTS = ("stop", "watch-stop")
FREQUENT_EVENTS = ("tool", "watch-active")
# When an AI says it is being asked too often: the first pause if it does not say how long,
# doubled for each refusal in a row, up to the longest pause.
DEFAULT_BACKOFF_SECONDS = 120.0
MAX_BACKOFF_SECONDS = 30 * 60.0

OUTSIDE = "__outside__"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


class AgentInfo(FrozenModel):
    """A chat connected to Studio."""

    chat_id: str = Field(description="The chat's id")
    agent_id: str = Field(
        default="", description="The id of the agent currently running the chat"
    )
    name: str = Field(description="The chat's title")
    agent_type: str = Field(description="The kind of AI running it")
    account: str | None = Field(description="The account it signs in with")
    state: str = Field(
        default="", description="What Studio says it is doing, e.g. RUNNING or WAITING"
    )
    agent_activity_time: str | None = Field(
        default=None, description="When Studio last saw it do something"
    )
    built_in: bool = Field(
        default=False,
        description="Whether it is an app's own assistant (e.g. the Files app's) and not a chat the user made",
    )


class Source(FrozenModel):
    """One (kind of AI, account) pair whose usage is read."""

    key: str = Field(description="'<agent type>:<account>'")
    provider: UsageProvider = Field(description="Where this kind of AI reports usage")
    account: str = Field(description="The account id")
    account_label: str = Field(description="What the account is called")


def source_key(agent_type: str, account: str) -> str:
    return f"{agent_type}:{account}"


def source_key_for(agent: "AgentInfo") -> str | None:
    """The source a chat's usage belongs to, or None when its kind of AI has no known source."""
    provider = PROVIDERS.get(agent.agent_type)
    if provider is None:
        return None
    account = provider.account_key(agent.account)
    return source_key(agent.agent_type, account) if account else None


def account_labels() -> dict[str, str]:
    try:
        index = json.loads((base.ACCOUNTS_DIR / "index.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        a["id"]: a.get("display") or a.get("name") or a["id"]
        for a in index.get("accounts", [])
        if "id" in a
    }


HANDOFF_MARKER = "The user's message, which you are answering now:"
SYSTEM_MESSAGE_PREFIXES = (
    "The following is a <SYSTEM_MESSAGE>",
    "<SYSTEM_MESSAGE>",
)


def clean_prompt(text: str | None) -> str | None:
    """Extracts the user's own message when Studio wrapped it in a handoff or system notice."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if HANDOFF_MARKER in cleaned:
        cleaned = cleaned.split(HANDOFF_MARKER, 1)[1].strip()
    elif cleaned.startswith("You are continuing the chat "):
        return None
    if not cleaned or cleaned.startswith(SYSTEM_MESSAGE_PREFIXES):
        return None
    return cleaned


def prompt_key(prompt: str) -> str:
    """What a message's summary is filed under."""
    return hashlib.sha256(prompt.encode()).hexdigest()


def summarise_prompt(prompt: str) -> str | None:
    """A short AI-written label for one message, or None when the AI could not be asked."""
    try:
        result = claude_p_completion(
            f"Label this request:\n\n<request>\n{prompt}\n</request>",
            system=SUMMARY_SYSTEM,
            model=SUMMARY_MODEL,
            timeout_seconds=SUMMARY_TIMEOUT_SECONDS,
        )
    except (ClaudeCLIError, subprocess.SubprocessError, OSError) as error:
        logger.warning("Couldn't summarise a message: {}", error)
        return None
    lines = result.text.strip().splitlines()
    summary = lines[0].strip().strip('"').rstrip(".") if lines else ""
    return summary[:SUMMARY_MAX_CHARS] or None


def latest_user_message(agent_id: str) -> str | None:
    """The newest message the user sent this agent, from Studio's record of the chat (any kind of AI)."""
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "--no-sync",
                "mngr",
                "transcript",
                agent_id,
                "--role",
                "user",
                "--tail",
                "10",
                "--format",
                "jsonl",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd="/home/user/workspace",
        )
    except (subprocess.SubprocessError, OSError) as error:
        logger.warning("Couldn't read the chat's latest message: {}", error)
        return None
    for line in reversed(result.stdout.splitlines()):
        try:
            message = clean_prompt(json.loads(line).get("message"))
        except (json.JSONDecodeError, AttributeError):
            continue
        if message:
            return message
    return None


def time_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return parse_time(value)
    except ValueError:
        return None


def same_reset_time(one: str | None, other: str | None) -> bool:
    """Whether two readings name the same reset of a limit.

    An AI can report one reset a fraction of a second differently on each reading (Claude
    does), so times this close are the same reset, not a new window.
    """
    if one is None or other is None:
        return one is other
    if one == other:
        return True
    try:
        return abs(parse_time(one) - parse_time(other)) <= RESET_TIME_TOLERANCE_SECONDS
    except ValueError:
        return False


# Agent types Studio lists that are not a chat with an AI.
NON_CHAT_AGENT_TYPES = ("main", "room")


def chat_id_of(agent: dict[str, Any]) -> str | None:
    """The chat a listed agent runs, or None when the agent is not a chat.

    A chat that moved from one AI to another carries its id as a label on each of its
    agents. A chat that has only ever run on one agent carries no such label: the chat's
    id is that agent's own id, which is also the id its hooks report activity under.
    """
    labels = agent.get("labels") or {}
    if labels.get("chat_id"):
        return str(labels["chat_id"])
    if str(agent.get("type") or "") in NON_CHAT_AGENT_TYPES:
        return None
    if not labels.get("display_name") or not agent.get("id"):
        return None
    return str(agent["id"])


def watched_event(before: AgentInfo | None, now: AgentInfo) -> str | None:
    """What changed for a chat between two looks at Studio's chat list, as an activity event."""
    was_running = before is not None and before.state == "RUNNING"
    if now.state == "RUNNING" and not was_running:
        return "watch-start"
    if was_running and now.state != "RUNNING":
        return "watch-stop"
    if (
        now.state == "RUNNING"
        and before is not None
        and now.agent_activity_time != before.agent_activity_time
    ):
        return "watch-active"
    return None


class WorkWindow(FrozenModel):
    """A stretch during which one chat was working."""

    start: float = Field(description="Start, seconds since the epoch")
    end: float = Field(description="End, seconds since the epoch")
    started_at: str = Field(description="Start (ISO 8601)")
    label: str = Field(description="The message that started it")


class TurnCredit(FrozenModel):
    """Usage credited to one stretch of a chat's work."""

    chat_id: str = Field(description="The chat")
    started_at: str = Field(description="When the stretch began (ISO 8601)")
    label: str = Field(
        description="An AI-written summary of the message that started it; the message itself, shortened, until one is written"
    )
    prompt: str | None = Field(
        default=None, description="The message that started it, verbatim, if known"
    )
    summarised: bool = Field(
        default=False, description="Whether the label is an AI-written summary"
    )
    credits: dict[str, float] = Field(description="Percent credited per limit key")


class PastTurn(FrozenModel):
    """One stretch of a chat's work and what it was credited in a past window of one limit."""

    chat_id: str = Field(description="The chat")
    started_at: str = Field(description="When the stretch began (ISO 8601)")
    label: str = Field(description="The stretch's label, as on the current graph")
    prompt: str | None = Field(default=None, description="The message that started it, verbatim, if known")
    summarised: bool = Field(default=False, description="Whether the label is an AI-written summary")
    credit: float = Field(description="Percent of the limit credited to it")


class PastWindow(FrozenModel):
    """One limit's window that has since reset: its graph and log as they stood at the reset."""

    started_at: str = Field(description="When the window began, or when tracking (or reporting) began (ISO 8601)")
    ended_at: str = Field(description="When it reset (ISO 8601)")
    level: float = Field(description="The highest percent the AI reported for the limit in the window")
    series: dict[str, list[tuple[str, float]]] = Field(description="chat -> running total points")
    totals: dict[str, float] = Field(description="chat -> total percent credited")
    turns: list[PastTurn] = Field(description="Credited stretches, newest first")


class Attribution(FrozenModel):
    """Every chat's credited usage, recomputed from the logs."""

    series: dict[str, dict[str, list[tuple[str, float]]]] = Field(
        description="chat -> limit -> running total points since the limit last reset"
    )
    totals: dict[str, dict[str, float]] = Field(
        description="chat -> limit -> total percent since the limit last reset"
    )
    turns: dict[str, list[TurnCredit]] = Field(
        description="chat -> credited stretches, newest first"
    )
    window_starts: dict[str, str] = Field(
        default_factory=dict,
        description="limit -> when its current window began (ISO 8601), for a limit seen to reset",
    )
    past_windows: dict[str, list[PastWindow]] = Field(
        default_factory=dict,
        description="limit -> its windows that have reset since tracking began, oldest first",
    )
    first_read: dict[str, str] = Field(
        default_factory=dict,
        description="limit -> when a reading first named it (ISO 8601), for a limit the AI started reporting after tracking began",
    )


class UsageStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.readings_path = data_dir / "readings.jsonl"
        self.activity_path = data_dir / "activity.jsonl"
        self.summaries_path = data_dir / "summaries.jsonl"
        self._lock = threading.Lock()
        self._summariser = summarise_prompt
        self._summary_pool = ThreadPoolExecutor(
            max_workers=SUMMARY_WORKERS, thread_name_prefix="summariser"
        )
        # Messages being summarised now, or that failed since the tracker started (retried on the next start).
        self._summaries_asked: set[str] = set()
        self._reading_locks: dict[str, threading.Lock] = {}
        self._last_reading_at: dict[str, float] = {}
        # A pause an AI asked for is kept in the readings log, so a restart honors it.
        self._backoff_until: dict[str, float] = {}
        self._refusals_in_a_row: dict[str, int] = {}
        for record in self.readings():
            key = record.get("source_key")
            if not key:
                continue
            if record.get("status") == 429:
                self._refusals_in_a_row[key] = self._refusals_in_a_row.get(key, 0) + 1
                if record.get("backoff_until"):
                    self._backoff_until[key] = parse_time(record["backoff_until"])
            elif record.get("status") == 200:
                self._refusals_in_a_row[key] = 0
        self._summaries: dict[str, str] = {
            record["key"]: record["summary"]
            for record in self._read(self.summaries_path)
            if record.get("key") and record.get("summary")
        }
        self.last_error: dict[str, str] = {}
        self._agents: list[AgentInfo] = []
        self._agents_at = 0.0
        self._agents_lock = threading.Lock()

    # ----- logs -----

    def _append(self, path: Path, record: dict[str, Any]) -> None:
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(json.dumps(record) + "\n")

    def _read(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records = []
        with self._lock:
            lines = path.read_text().splitlines()
        for line in lines:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping an unreadable line in {}", path)
        return records

    def readings(self) -> list[dict[str, Any]]:
        return self._read(self.readings_path)

    def activity(self) -> list[dict[str, Any]]:
        return self._read(self.activity_path)

    def source_readings(self, key: str) -> list[dict[str, Any]]:
        """Every reading of one source, oldest first.

        Readings taken before sources existed name the endpoint instead of a source; they
        belong to the one Claude account this workspace had then (Claude was the only AI read then).
        """
        legacy = self._legacy_source_key()
        return [r for r in self.readings() if r.get("source_key", legacy) == key]

    def latest_reading(self, key: str) -> dict[str, Any] | None:
        good = [r for r in self.source_readings(key) if r.get("status") == 200]
        return good[-1] if good else None

    def _legacy_source_key(self) -> str | None:
        claude_accounts = (
            sorted(p.parent.name for p in base.ACCOUNTS_DIR.glob("*/.credentials.json"))
            if base.ACCOUNTS_DIR.is_dir()
            else []
        )
        return (
            source_key("claude", claude_accounts[0])
            if len(claude_accounts) == 1
            else None
        )

    # ----- summaries -----

    def summary_for(self, prompt: str) -> str | None:
        """The summary written for a message, if one has been."""
        return self._summaries.get(prompt_key(prompt))

    def request_summary(self, prompt: str) -> None:
        """Have a message summarised in the background, unless it already was or is too short to need it."""
        key = prompt_key(prompt)
        with self._lock:
            if (
                len(prompt) <= SUMMARY_MIN_CHARS
                or key in self._summaries
                or key in self._summaries_asked
            ):
                return
            self._summaries_asked.add(key)
        self._summary_pool.submit(self._write_summary, key, prompt)

    def _write_summary(self, key: str, prompt: str) -> None:
        summary = self._summariser(prompt)
        if summary is None:
            return
        self._append(
            self.summaries_path,
            {"t": now_iso(), "key": key, "summary": summary, "model": SUMMARY_MODEL},
        )
        self._summaries[key] = summary
        self._summaries_asked.discard(key)

    def request_missing_summaries(self) -> None:
        """Ask for a summary of every recorded message that has none yet."""
        for record in self.activity():
            prompt = clean_prompt(record.get("prompt"))
            if prompt:
                self.request_summary(prompt)

    # ----- activity -----

    def record_activity(
        self,
        chat_id: str,
        event: str,
        hook_input: dict[str, Any],
        at: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "t": at or now_iso(),
            "chat_id": chat_id,
            "event": event,
        }
        if event in START_EVENTS:
            prompt = clean_prompt(hook_input.get("prompt"))
            if prompt:
                record["prompt"] = prompt[:2000]
                self.request_summary(record["prompt"])
        self._append(self.activity_path, record)
        agent = next((a for a in self.agents() if a.chat_id == chat_id), None)
        sources = self.sources()
        if agent is not None:
            sources = [s for s in sources if s.key == source_key_for(agent)]
        gap = (
            TOOL_READING_MIN_GAP_SECONDS
            if event in FREQUENT_EVENTS
            else TURN_READING_MIN_GAP_SECONDS
        )
        for source in sources:
            if time.time() - self._last_reading_at.get(source.key, 0.0) >= gap:
                # Stamp the reading at the event's time, so a reading taken right as a turn
                # begins is not counted as part of that turn.
                threading.Thread(
                    target=self.take_reading,
                    args=(source,),
                    kwargs={
                        "trigger": {"chat_id": chat_id, "event": event},
                        "at": record["t"],
                    },
                    daemon=True,
                ).start()

    # ----- readings -----

    def take_all_readings(self, trigger: dict[str, Any], wait: bool = False) -> None:
        for source in self.sources():
            self.take_reading(source, trigger=trigger, wait=wait)

    def take_reading(
        self,
        source: Source,
        trigger: dict[str, Any] | None = None,
        at: str | None = None,
        wait: bool = False,
    ) -> dict[str, Any] | None:
        """Ask one source for current usage and log the answer. Returns the logged record, or None if skipped.

        A reading of the same source already in flight makes this one skip, unless ``wait`` is set.
        """
        lock = self._reading_locks.setdefault(source.key, threading.Lock())
        if not lock.acquire(blocking=wait, timeout=30 if wait else -1):
            return None
        provider = source.provider
        try:
            if time.time() < self._backoff_until.get(source.key, 0.0):
                # The AI asked to be left alone for a while; skip until then.
                return None
            self._last_reading_at[source.key] = time.time()
            credentials = provider.credentials(source.account)
            token = (
                provider.access_token(credentials) if credentials is not None else None
            )
            if token is None:
                self.last_error[source.key] = (
                    f"No {provider.label} sign-in found for this account yet. It appears once a chat on it has run."
                )
                return None
            request = urllib.request.Request(
                provider.usage_url,
                data=provider.request_body,
                headers={
                    "User-Agent": "ai-usage-limits/1.0",
                    **(provider.request_headers(credentials) if credentials else {}),
                    "Authorization": f"Bearer {token}",
                },
            )
            record: dict[str, Any] = {
                "t": at or now_iso(),
                "taken_at": now_iso(),
                "trigger": trigger,
                "source_key": source.key,
                "source": provider.usage_url,
            }
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    record["status"] = response.status
                    record["raw"] = json.loads(response.read().decode())
                if isinstance(record["raw"], dict):
                    self._read_extras(provider, request, record)
                self.last_error.pop(source.key, None)
                self._refusals_in_a_row[source.key] = 0
            except urllib.error.HTTPError as error:
                record["status"] = error.code
                record["error"] = error.read().decode(errors="replace")[:1000]
                if error.code == 429:
                    retry_after = error.headers.get("Retry-After", "")
                    refusals = self._refusals_in_a_row.get(source.key, 0) + 1
                    self._refusals_in_a_row[source.key] = refusals
                    asked = float(retry_after) if retry_after.isdigit() else 0.0
                    pause_seconds = min(
                        MAX_BACKOFF_SECONDS,
                        max(asked, DEFAULT_BACKOFF_SECONDS * 2 ** (refusals - 1)),
                    )
                    self._backoff_until[source.key] = time.time() + pause_seconds
                    resume = datetime.fromtimestamp(
                        time.time() + pause_seconds, timezone.utc
                    ).isoformat()
                    record["backoff_until"] = resume
                    self.last_error[source.key] = (
                        f"{provider.label} asked to be checked less often; the numbers below are from the last check, "
                        f"and checking resumes in about {max(1, round(pause_seconds / 60))} min."
                    )
                elif error.code == 401:
                    self.last_error[source.key] = (
                        f"{provider.label}'s sign-in here has expired; it renews the next time a chat on this account runs."
                    )
                else:
                    self.last_error[source.key] = (
                        f"{provider.label} answered with an error ({error.code})."
                    )
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                record["status"] = 0
                record["error"] = str(error)[:1000]
                self.last_error[source.key] = (
                    f"Couldn't reach {provider.label} to check usage."
                )
            self._append(self.readings_path, record)
            return record
        finally:
            lock.release()

    def _read_extras(
        self,
        provider: UsageProvider,
        request: urllib.request.Request,
        record: dict[str, Any],
    ) -> None:
        """Ask the provider's other endpoints and keep each reply inside the reading, under its key.

        One that fails is noted and left out; the reading still stands on the main reply.
        """
        for name, url in provider.extra_usage_urls.items():
            extra = urllib.request.Request(
                url, data=provider.request_body, headers=dict(request.header_items())
            )
            try:
                with urllib.request.urlopen(extra, timeout=20) as response:
                    record["raw"][name] = json.loads(response.read().decode())
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                record.setdefault("extra_errors", {})[name] = str(error)[:500]

    # ----- chats and sources -----

    def agents(self) -> list[AgentInfo]:
        """The chats connected to Studio in this workspace.

        Answers from the last listing at once; a stale listing is refreshed in the background.
        """
        if time.time() - self._agents_at >= 60 and self._agents_lock.acquire(
            blocking=False
        ):
            threading.Thread(target=self._refresh_agents, daemon=True).start()
        return self._agents

    def refresh_agents_now(self) -> None:
        with self._agents_lock:
            self._list_agents()

    def _refresh_agents(self) -> None:
        try:
            self._list_agents()
        finally:
            self._agents_lock.release()

    def _list_agents(self) -> None:
        try:
            result = subprocess.run(
                ["uv", "run", "--no-sync", "mngr", "list", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd="/home/user/workspace",
            )
            parsed = json.loads(result.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as error:
            logger.warning("Couldn't list chats: {}", error)
            return
        finally:
            self._agents_at = time.time()
        listed = parsed.get("agents", []) if isinstance(parsed, dict) else parsed
        # A chat that moved from one AI to another has an archived entry per earlier AI plus
        # its current one, ordered by chat_seq; put the current entry first so its title and
        # running state win, and keep one entry per (chat, AI, account) so both sources stay shown.
        ordered = sorted(
            (a for a in listed if isinstance(a, dict)),
            key=lambda a: int((a.get("labels") or {}).get("chat_seq") or 0),
            reverse=True,
        )
        previous = {(a.chat_id, a.agent_type, a.account): a for a in self._agents}
        chat_names: dict[str, str] = {}
        agents: dict[tuple[str, str, str | None], AgentInfo] = {}
        for agent in ordered:
            labels = agent.get("labels") or {}
            chat_id = chat_id_of(agent)
            if chat_id is None:
                continue
            raw_name = str(labels.get("display_name") or agent.get("name") or chat_id)
            chat_names.setdefault(chat_id, raw_name.split(" (archived ")[0])
            agent_type = str(agent.get("type") or "")
            account = labels.get("account")
            key = (chat_id, agent_type, account)
            if key in agents:
                continue
            agents[key] = AgentInfo(
                chat_id=chat_id,
                name=chat_names[chat_id],
                agent_id=str(agent.get("id") or ""),
                agent_type=agent_type,
                account=account,
                state=str(agent.get("state") or ""),
                agent_activity_time=agent.get("agent_activity_time"),
                built_in=bool(labels.get("app_chat")),
            )
        self._agents = list(agents.values())
        for agent_key, agent in agents.items():
            event = watched_event(previous.get(agent_key), agent)
            if event == "watch-start":
                # Label the task with the message that started it, read from the chat's record in
                # Studio (the same for every kind of AI). That takes a moment, so it runs aside.
                threading.Thread(
                    target=self._record_start_with_message,
                    args=(agent, now_iso()),
                    daemon=True,
                ).start()
            elif event is not None:
                self.record_activity(agent.chat_id, event, {})

    def _record_start_with_message(self, agent: AgentInfo, at: str) -> None:
        message = latest_user_message(agent.agent_id) if agent.agent_id else None
        self.record_activity(
            agent.chat_id, "watch-start", {"prompt": message} if message else {}, at=at
        )

    def sources(self) -> list[Source]:
        """One source per (kind of AI, account) among the connected chats whose kind has a known provider."""
        labels = account_labels()
        sources: dict[str, Source] = {}
        for agent in self.agents():
            provider = PROVIDERS.get(agent.agent_type)
            key = source_key_for(agent)
            if provider is None or key is None:
                continue
            account = key.split(":", 1)[1]
            sources.setdefault(
                key,
                Source(
                    key=key,
                    provider=provider,
                    account=account,
                    account_label=labels.get(
                        account,
                        f"{provider.label} sign-in"
                        if account in ("chatgpt", "google")
                        else account,
                    ),
                ),
            )
        return list(sources.values())

    # ----- crediting -----

    def work_windows(self) -> dict[str, list[WorkWindow]]:
        """Per chat, the stretches it was working."""
        now = time.time()
        windows: dict[str, list[WorkWindow]] = {}
        # chat id -> (start, last seen, start text, label) of the stretch still going
        open_windows: dict[str, tuple[float, float, str, str]] = {}
        for record in sorted(self.activity(), key=lambda r: r["t"]):
            chat_id, event, t = (
                str(record["chat_id"]),
                record["event"],
                parse_time(record["t"]),
            )
            current = open_windows.pop(chat_id, None)
            if current is not None and t - current[1] > IDLE_CUTOFF_SECONDS:
                windows.setdefault(chat_id, []).append(
                    WorkWindow(
                        start=current[0],
                        end=current[1] + 60,
                        started_at=current[2],
                        label=current[3],
                    )
                )
                current = None
            prompt = clean_prompt(record.get("prompt"))
            if current is None and event in STOP_EVENTS:
                # The stretch already ended (both a chat's own report and Studio saw it finish).
                continue
            if current is None:
                current = (
                    t,
                    t,
                    str(record["t"]),
                    prompt or NO_MESSAGE_LABEL,
                )
            elif event in START_EVENTS and prompt:
                # A new message while still working: keep the stretch, but note the newest request.
                current = (current[0], current[1], current[2], prompt)
            current = (current[0], t, current[2], current[3])
            if event in STOP_EVENTS:
                windows.setdefault(chat_id, []).append(
                    WorkWindow(
                        start=current[0], end=t, started_at=current[2], label=current[3]
                    )
                )
            else:
                open_windows[chat_id] = current
        for chat_id, current in open_windows.items():
            end = now if now - current[1] <= IDLE_CUTOFF_SECONDS else current[1] + 60
            windows.setdefault(chat_id, []).append(
                WorkWindow(
                    start=current[0], end=end, started_at=current[2], label=current[3]
                )
            )
        return windows

    def attribute(self, source: Source) -> Attribution:
        """Credit each increase in one source's readings to that source's chats that were working.

        Each limit's credits count from its last reset: when a limit resets, every chat's
        running total, points and credited stretches for that limit start over.
        """
        readings = sorted(
            (r for r in self.source_readings(source.key) if r.get("status") == 200),
            key=lambda r: r["t"],
        )
        members = {a.chat_id for a in self.agents() if source_key_for(a) == source.key}
        windows = {
            chat_id: w
            for chat_id, w in self.work_windows().items()
            if chat_id in members
        }
        series: dict[str, dict[str, list[tuple[str, float]]]] = {}
        totals: dict[str, dict[str, float]] = {}
        turns: dict[tuple[str, float], dict[str, Any]] = {}
        window_starts: dict[str, str] = {}
        seen: dict[str, base.Limit] = (
            {
                limit.key: limit
                for limit in source.provider.parse_limits(readings[0]["raw"])
            }
            if readings
            else {}
        )
        # Only the limits a later reading named first: the rest have been read since tracking began.
        first_read: dict[str, str] = {}
        past_windows: dict[str, list[PastWindow]] = {}
        # limit -> when its current window began, as far as the readings show
        current_start: dict[str, str] = {key: readings[0]["t"] for key in seen}
        for before, after in zip(readings, readings[1:]):
            start, end = parse_time(before["t"]), parse_time(after["t"])
            if end <= start:
                continue
            increases: dict[str, float] = {}
            for limit in source.provider.parse_limits(after["raw"]):
                previous = seen.get(limit.key)
                if previous is None:
                    seen[limit.key] = limit
                    first_read[limit.key] = after["t"]
                    current_start[limit.key] = after["t"]
                    continue
                new_reset = time_or_none(limit.resets_at)
                old_reset = time_or_none(previous.resets_at)
                if (
                    new_reset is not None
                    and old_reset is not None
                    and new_reset < old_reset - RESET_TIME_TOLERANCE_SECONDS
                    and limit.percent > previous.percent
                ):
                    # A stale answer from before a reset: more used and an earlier reset time than the window
                    # already seen. A real window never moves its reset earlier while usage grows; skip it.
                    continue
                if limit.resets_at is not None and same_reset_time(
                    limit.resets_at, previous.resets_at
                ):
                    increase = max(0.0, limit.percent - previous.percent)
                    if limit.percent > previous.percent:
                        seen[limit.key] = limit
                elif (
                    limit.percent >= previous.percent
                    and limit.resets_at is None
                    and previous.resets_at is None
                ):
                    increase = limit.percent - previous.percent
                    seen[limit.key] = limit
                elif (
                    limit.percent >= previous.percent
                    and (previous_reset := time_or_none(previous.resets_at)) is not None
                    and time_or_none(limit.resets_at) is not None
                    and end < previous_reset - RESET_TIME_TOLERANCE_SECONDS
                ):
                    # The reset time moved before the old one arrived and nothing was given back: the
                    # same window, re-timed (an unused limit's reset is counted from each reading).
                    increase = limit.percent - previous.percent
                    seen[limit.key] = limit
                else:
                    # The limit reset to a new window: everything now counted was used since the reset,
                    # and what was credited before it no longer counts toward the limit.
                    increase = limit.percent
                    seen[limit.key] = limit
                    reset_at = time_or_none(previous.resets_at)
                    new_start = (
                        str(previous.resets_at)
                        if reset_at is not None and start <= reset_at <= end
                        else after["t"]
                    )
                    key = limit.key
                    past_windows.setdefault(key, []).append(
                        PastWindow(
                            started_at=current_start.get(key, readings[0]["t"]),
                            ended_at=new_start,
                            level=previous.percent,
                            series={c: list(by_key[key]) for c, by_key in series.items() if key in by_key},
                            totals={c: by_key[key] for c, by_key in totals.items() if key in by_key},
                            turns=[
                                PastTurn(
                                    chat_id=turn["chat_id"],
                                    started_at=turn["started_at"],
                                    label=turn["label"],
                                    prompt=turn["prompt"],
                                    summarised=turn["summarised"],
                                    credit=turn["credits"][key],
                                )
                                for turn in sorted(turns.values(), key=lambda t: t["started_at"], reverse=True)
                                if turn["credits"].get(key, 0) > 0
                            ],
                        )
                    )
                    window_starts[key] = new_start
                    current_start[key] = new_start
                    for totals_before in totals.values():
                        totals_before.pop(limit.key, None)
                    for series_before in series.values():
                        series_before.pop(limit.key, None)
                    for turn_before in turns.values():
                        turn_before["credits"].pop(limit.key, None)
                if increase > 0:
                    increases[limit.key] = increase
            if not increases:
                continue
            working: list[tuple[str, WorkWindow]] = []
            for chat_id, chat_windows in windows.items():
                for window in chat_windows:
                    if window.start < end and window.end > start:
                        working.append((chat_id, window))
                        break
            if not working:
                working = [
                    (
                        OUTSIDE,
                        WorkWindow(
                            start=start, end=end, started_at=before["t"], label=""
                        ),
                    )
                ]
            share = 1.0 / len(working)
            for chat_id, window in working:
                chat_totals = totals.setdefault(chat_id, {})
                chat_series = series.setdefault(chat_id, {})
                turn: dict[str, Any] | None = turns.get((chat_id, window.start))
                if turn is None:
                    prompt = (
                        window.label
                        if window.label and window.label != NO_MESSAGE_LABEL
                        else None
                    )
                    summary = self.summary_for(prompt) if prompt else None
                    turn = turns[(chat_id, window.start)] = {
                        "chat_id": chat_id,
                        "started_at": window.started_at,
                        "label": summary or window.label[:PROMPT_LABEL_MAX_CHARS],
                        "prompt": prompt,
                        "summarised": summary is not None,
                        "credits": {},
                    }
                for key, increase in increases.items():
                    credited = increase * share
                    chat_totals[key] = chat_totals.get(key, 0.0) + credited
                    chat_series.setdefault(key, []).append(
                        (after["t"], round(chat_totals[key], 3))
                    )
                    turn["credits"][key] = turn["credits"].get(key, 0.0) + credited
        by_chat: dict[str, list[TurnCredit]] = {}
        for turn in sorted(turns.values(), key=lambda t: t["started_at"], reverse=True):
            if turn["credits"]:
                by_chat.setdefault(turn["chat_id"], []).append(TurnCredit(**turn))
        return Attribution(
            series=series,
            totals=totals,
            turns=by_chat,
            window_starts=window_starts,
            first_read=first_read,
            past_windows=past_windows,
        )


def chats_to_show(
    members: list[AgentInfo], attribution: Attribution
) -> list[AgentInfo]:
    """The chats of one source worth a tab: every chat the user made, and an app's own assistant only once it has used something."""
    return [
        agent
        for agent in members
        if not agent.built_in
        or any(v > 0 for v in attribution.totals.get(agent.chat_id, {}).values())
    ]


def start_background_poller(
    store: UsageStore, interval_seconds: float
) -> threading.Event:
    """Take a reading now and every ``interval_seconds``, so usage outside these chats is not credited to them.

    Returns the event that stops the poller when set.
    """
    stopped = threading.Event()

    def loop() -> None:
        # The sources come from the chats connected to Studio, so list them before the first reading.
        store.refresh_agents_now()
        store.request_missing_summaries()
        keep_polling = True
        while keep_polling:
            try:
                store.take_all_readings(trigger={"chat_id": None, "event": "scheduled"})
            except (OSError, ValueError, KeyError):
                logger.exception("Scheduled usage reading failed")
            keep_polling = not stopped.wait(interval_seconds)

    def watch() -> None:
        keep_watching = True
        while keep_watching:
            try:
                store.refresh_agents_now()
            except (OSError, ValueError, KeyError):
                logger.exception("Looking at the chat list failed")
            keep_watching = not stopped.wait(WATCH_INTERVAL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="usage-poller").start()
    threading.Thread(target=watch, daemon=True, name="chat-watcher").start()
    return stopped
