"""The control lease (docs/system/specs/screen/spec.md sections 6.1 and 6.2): one controller at a time, human or
agent; any human may take control, agents never preempt a human and queue behind each other; the browser app's
state machine, with the Screen's one human session in place of the resting human."""

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.mutable_model import MutableModel
from screen.data_types import AcquireStatus
from screen.data_types import ControlRecord
from screen.data_types import ControlView
from screen.data_types import Controller
from screen.data_types import QueuedAgent
from screen.interfaces import AgentMessengerInterface
from screen.primitives import AgentId
from screen.primitives import NekoSessionId

# An agent's lease has no live connection to tell a dead owner from a thinking one, so it frees itself after this
# long with no command; the human's take-control is the instant escape hatch.
LEASE_IDLE_TTL_SECONDS: Final[float] = 60.0
# A queued agent handed the Screen is messaged to resume; if it sends nothing within this window the grant passes on.
CLAIM_WINDOW_SECONDS: Final[float] = 12.0

CONTROL_RECORD_FILENAME: Final[str] = "control.json"


class _Waiter:
    """One agent parked in the FIFO wait queue, woken when granted or evicted."""

    def __init__(self, agent_id: AgentId, agent_name: str | None) -> None:
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.event = threading.Event()
        self.is_granted = False


class ScreenControl(MutableModel):
    """The lease: its state, its two queues, its clocks, and its projection hooks."""

    model_config = {"arbitrary_types_allowed": True}

    messenger: AgentMessengerInterface = Field(frozen=True, description="Wakes queued agents through their chats")
    on_changed: Callable[[ControlView], None] = Field(
        frozen=True, description="Called after every change, outside the lock, with the new view"
    )
    on_handoff_request: Callable[[str, str], None] = Field(
        frozen=True, description="Called with the agent's name and reason when it hands the Screen to the human"
    )
    record_path: Path | None = Field(frozen=True, description="Where the lease is checkpointed; None for none")
    clock: Callable[[], float] = Field(default=time.monotonic, frozen=True, description="The monotonic clock")

    controller: Controller = Field(default=Controller.NOBODY, description="Who drives the Screen")
    owner_agent_id: AgentId | None = Field(default=None, description="The agent holding the lease")
    owner_agent_name: str | None = Field(default=None, description="That agent's display name")
    human_pinned: bool = Field(default=False, description="Whether the human took control explicitly")
    human_session_id: NekoSessionId | None = Field(default=None, description="The human controller's neko session")
    human_name: str | None = Field(default=None, description="That human's display name")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _resume_queue: list[QueuedAgent] = PrivateAttr(default_factory=list)
    _wait_queue: list[_Waiter] = PrivateAttr(default_factory=list)
    _lease_touched_at: float = PrivateAttr(default=0.0)
    _granted_at: float = PrivateAttr(default=0.0)

    # reading

    def view(self) -> ControlView:
        with self._lock:
            return self._view_locked()

    def _view_locked(self) -> ControlView:
        idle_seconds: int | None = None
        idle_release_seconds: int | None = None
        if self.controller == Controller.AGENT and self._lease_touched_at:
            idle = self.clock() - self._lease_touched_at
            idle_seconds = max(0, int(idle))
            idle_release_seconds = max(0, int(LEASE_IDLE_TTL_SECONDS - idle))
        return ControlView(
            controller=self.controller,
            human_pinned=self.human_pinned,
            human_session_id=self.human_session_id,
            human_name=self.human_name,
            agent_id=self.owner_agent_id,
            agent_name=self.owner_agent_name,
            waiting=tuple(self._waiting_names_locked()),
            idle_seconds=idle_seconds,
            idle_release_seconds=idle_release_seconds,
        )

    def _waiting_names_locked(self) -> list[str]:
        names = [queued.agent_name or str(queued.agent_id) for queued in self._resume_queue]
        names += [waiter.agent_name or str(waiter.agent_id) for waiter in self._wait_queue]
        return names

    def resume_queue(self) -> list[QueuedAgent]:
        with self._lock:
            return list(self._resume_queue)

    def waiting_agent_ids(self) -> list[AgentId]:
        with self._lock:
            return [waiter.agent_id for waiter in self._wait_queue]

    def is_grant_pending(self) -> bool:
        with self._lock:
            return bool(self._granted_at)

    def record(self) -> ControlRecord:
        with self._lock:
            return self._record_locked()

    def _record_locked(self) -> ControlRecord:
        return ControlRecord(
            controller=self.controller,
            owner_agent_id=self.owner_agent_id,
            owner_agent_name=self.owner_agent_name,
            human_pinned=self.human_pinned,
            human_session_id=self.human_session_id,
            human_name=self.human_name,
            resume_queue=tuple(self._resume_queue),
        )

    # persistence

    def restore(self) -> None:
        """Load the checkpointed lease, if any; an unreadable file is logged and left behind."""
        if self.record_path is None or not self.record_path.is_file():
            return
        try:
            loaded = ControlRecord.model_validate_json(self.record_path.read_text())
        except (OSError, ValidationError) as e:
            logger.warning("Ignored an unreadable lease checkpoint at {}: {}", self.record_path, e)
            return
        with self._lock:
            self.controller = loaded.controller
            self.owner_agent_id = loaded.owner_agent_id
            self.owner_agent_name = loaded.owner_agent_name
            self.human_pinned = loaded.human_pinned
            self.human_session_id = loaded.human_session_id
            self.human_name = loaded.human_name
            self._resume_queue = list(loaded.resume_queue)
            if self.controller == Controller.AGENT:
                self._lease_touched_at = self.clock()

    def _checkpoint_locked(self) -> None:
        if self.record_path is None:
            return
        try:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.record_path.with_suffix(self.record_path.suffix + ".tmp")
            temporary.write_text(json.dumps(self._record_locked().model_dump(mode="json"), indent=2))
            os.replace(temporary, self.record_path)
        except OSError as e:
            logger.warning("Could not checkpoint the lease to {}: {}", self.record_path, e)

    # the single writer

    def _write_locked(
        self,
        controller: Controller,
        agent_id: AgentId | None,
        agent_name: str | None,
        pinned: bool,
        human_session_id: NekoSessionId | None,
        human_name: str | None,
    ) -> None:
        self.controller = controller
        self.owner_agent_id = agent_id
        self.owner_agent_name = agent_name
        self.human_pinned = pinned
        self.human_session_id = human_session_id
        self.human_name = human_name
        if controller == Controller.AGENT:
            self._lease_touched_at = self.clock()
        else:
            self._granted_at = 0.0

    def _settle_queue_locked(self) -> list[QueuedAgent]:
        """Reconcile the queues with the state; returns the agents to wake (messaged outside the lock).

        A human in control evicts the connection-bound waiters (they never wait on a human) and keeps the resume
        queue, whose agents want the Screen back after the human is done. A free Screen goes to the first waiter,
        else to the first resume-queue agent, who is messaged and put on the claim clock.
        """
        if self.controller == Controller.HUMAN:
            waiters, self._wait_queue = self._wait_queue, []
            for waiter in waiters:
                waiter.is_granted = False
                waiter.event.set()
            return []
        if self.controller != Controller.NOBODY:
            return []
        if self._wait_queue:
            waiter = self._wait_queue.pop(0)
            self._dequeue_resume_locked(waiter.agent_id)
            self._write_locked(Controller.AGENT, waiter.agent_id, waiter.agent_name, False, None, None)
            waiter.is_granted = True
            waiter.event.set()
            return []
        if self._resume_queue:
            queued = self._resume_queue.pop(0)
            self._write_locked(Controller.AGENT, queued.agent_id, queued.agent_name, False, None, None)
            self._granted_at = self.clock()
            return [queued]
        return []

    def _enqueue_resume_locked(self, agent_id: AgentId, agent_name: str | None) -> None:
        if not any(queued.agent_id == agent_id for queued in self._resume_queue):
            self._resume_queue.append(QueuedAgent(agent_id=agent_id, agent_name=agent_name))

    def _enqueue_resume_front_locked(self, agent_id: AgentId, agent_name: str | None) -> None:
        self._resume_queue = [queued for queued in self._resume_queue if queued.agent_id != agent_id]
        self._resume_queue.insert(0, QueuedAgent(agent_id=agent_id, agent_name=agent_name))

    def _dequeue_resume_locked(self, agent_id: AgentId) -> None:
        self._resume_queue = [queued for queued in self._resume_queue if queued.agent_id != agent_id]

    def _finish(self, is_changed: bool, to_wake: list[QueuedAgent]) -> None:
        """After the lock is released: checkpoint and announce a change, and message the woken agents."""
        if is_changed:
            with self._lock:
                self._checkpoint_locked()
            view = self.view()
            logger.debug(
                "Lease changed: controller={} agent={} human={} pinned={} waiting={}",
                view.controller.value,
                view.agent_name or view.agent_id,
                view.human_name or view.human_session_id,
                view.human_pinned,
                list(view.waiting),
            )
            self.on_changed(view)
        for queued in to_wake:
            self.messenger.message_agent(
                str(queued.agent_id),
                queued.agent_name,
                "The Screen was handed back to you (the human finished with it). The human may have moved or "
                "resized windows and navigated the browser, so take a fresh `uv run --no-sync screen shot` (or re-`snapshot` with "
                "playwright-cli if you drive the browser) before continuing where you left off.",
            )

    # the verbs

    def acquire(
        self,
        agent_id: AgentId,
        agent_name: str | None,
        is_reclaim: bool,
        is_wait: bool,
        max_wait_seconds: float | None,
        is_enqueue_on_busy: bool,
    ) -> AcquireStatus:
        """Acquire the lease for an agent.

        ``ACQUIRED`` when the agent now holds it (a re-acquire by the holder is idempotent and refreshes its name);
        ``BUSY_HUMAN`` while a human controls the Screen (only ``is_reclaim``, the "the user told me to continue"
        verb, takes it); ``BUSY_AGENT`` when another agent holds it and the caller does not wait; ``TIMED_OUT``
        after ``max_wait_seconds`` in the wait queue. ``is_enqueue_on_busy`` adds a refused agent to the resume
        queue, so it is messaged when the Screen frees.
        """
        is_changed = False
        to_wake: list[QueuedAgent] = []
        waiter: _Waiter | None = None
        with self._lock:
            if self.controller == Controller.AGENT and self.owner_agent_id == agent_id:
                self.owner_agent_name = agent_name
                self._dequeue_resume_locked(agent_id)
                self._lease_touched_at = self.clock()
                self._granted_at = 0.0
                return AcquireStatus.ACQUIRED
            if self.controller == Controller.HUMAN and not is_reclaim:
                if is_enqueue_on_busy:
                    self._enqueue_resume_locked(agent_id, agent_name)
                    is_changed = True
                status = AcquireStatus.BUSY_HUMAN
            elif self.controller != Controller.AGENT:
                self._dequeue_resume_locked(agent_id)
                self._write_locked(Controller.AGENT, agent_id, agent_name, False, None, None)
                is_changed = True
                status = AcquireStatus.ACQUIRED
            elif not is_wait:
                if is_enqueue_on_busy:
                    self._enqueue_resume_locked(agent_id, agent_name)
                    is_changed = True
                status = AcquireStatus.BUSY_AGENT
            else:
                waiter = _Waiter(agent_id, agent_name)
                self._wait_queue.append(waiter)
                is_changed = True
                status = AcquireStatus.BUSY_AGENT
        self._finish(is_changed, to_wake)
        if waiter is None:
            return status
        return self._await_grant(waiter, max_wait_seconds)

    def _await_grant(self, waiter: _Waiter, max_wait_seconds: float | None) -> AcquireStatus:
        if waiter.event.wait(timeout=max_wait_seconds):
            # The thread that granted or evicted the waiter announced the change itself.
            return AcquireStatus.ACQUIRED if waiter.is_granted else AcquireStatus.BUSY_HUMAN
        is_changed = False
        to_wake: list[QueuedAgent] = []
        with self._lock:
            if waiter in self._wait_queue:
                self._wait_queue.remove(waiter)
                is_changed = True
            elif waiter.is_granted and self.controller == Controller.AGENT and self.owner_agent_id == waiter.agent_id:
                # Granted in the same instant it gave up: free the Screen rather than leave a no-show holding it.
                self._write_locked(Controller.NOBODY, None, None, False, None, None)
                to_wake = self._settle_queue_locked()
                is_changed = True
        self._finish(is_changed, to_wake)
        return AcquireStatus.TIMED_OUT

    def release(self, agent_id: AgentId) -> bool:
        """Release an agent's lease; only the holder can, and a double release is a safe no-op."""
        to_wake: list[QueuedAgent] = []
        with self._lock:
            if not (self.controller == Controller.AGENT and self.owner_agent_id == agent_id):
                return False
            self._write_locked(Controller.NOBODY, None, None, False, None, None)
            to_wake = self._settle_queue_locked()
        self._finish(True, to_wake)
        return True

    def touch(self, agent_id: AgentId) -> None:
        """Record the holder's activity: the idle clock restarts and a pending grant counts as claimed."""
        with self._lock:
            if self.controller == Controller.AGENT and self.owner_agent_id == agent_id:
                self._lease_touched_at = self.clock()
                self._granted_at = 0.0

    def take_control(self, session_id: NekoSessionId, name: str) -> bool:
        """A human takes control: always wins, preempting an agent (queued to resume first) or another human."""
        with self._lock:
            if self.controller == Controller.AGENT and self.owner_agent_id is not None:
                self._enqueue_resume_front_locked(self.owner_agent_id, self.owner_agent_name)
            self._write_locked(Controller.HUMAN, None, None, True, session_id, name)
            self._settle_queue_locked()
        self._finish(True, [])
        return True

    def return_to_agents(self, session_id: NekoSessionId, is_owner: bool) -> bool:
        """The human in control gives it up; the Screen goes to the first queued agent, else to nobody. A handoff
        nobody has claimed yet (the human's, with no session) is the owner's to give back."""
        to_wake: list[QueuedAgent] = []
        with self._lock:
            if self.controller != Controller.HUMAN:
                return False
            is_claimed_by_session = self.human_session_id == session_id
            is_unclaimed_handoff = self.human_session_id is None and is_owner
            if not (is_claimed_by_session or is_unclaimed_handoff):
                return False
            self._write_locked(Controller.NOBODY, None, None, False, None, None)
            to_wake = self._settle_queue_locked()
        self._finish(True, to_wake)
        return True

    def handoff(self, agent_id: AgentId, agent_name: str | None, reason: str) -> bool:
        """The holding agent hands the Screen to the human, pinned, and goes to the front of the resume queue: no
        agent takes it until the human gives it back. The human has no session yet; their first input, or Take
        control, fills it in with the pin kept."""
        with self._lock:
            if not (self.controller == Controller.AGENT and self.owner_agent_id == agent_id):
                return False
            self._enqueue_resume_front_locked(agent_id, agent_name)
            self._write_locked(Controller.HUMAN, None, None, True, None, None)
            self._settle_queue_locked()
        self._finish(True, [])
        self.on_handoff_request(agent_name or str(agent_id), reason)
        return True

    # what neko reports

    def human_became_host(self, session_id: NekoSessionId, name: str) -> bool:
        """neko made a human session the host without the service asking (an uncontrolled Screen's first input):
        record that human as the controller, unpinned, except that the first input after a handoff claims the
        handoff and keeps its pin."""
        with self._lock:
            if self.controller == Controller.AGENT:
                return False
            if self.controller == Controller.HUMAN and self.human_session_id == session_id:
                return False
            is_claiming_handoff = (
                self.controller == Controller.HUMAN and self.human_session_id is None and self.human_pinned
            )
            self._write_locked(Controller.HUMAN, None, None, is_claiming_handoff, session_id, name)
            self._settle_queue_locked()
        self._finish(True, [])
        return True

    def human_session_gone(self, session_id: NekoSessionId) -> bool:
        """The controlling human's session left (logged out, deleted): the Screen frees."""
        to_wake: list[QueuedAgent] = []
        with self._lock:
            if not (self.controller == Controller.HUMAN and self.human_session_id == session_id):
                return False
            self._write_locked(Controller.NOBODY, None, None, False, None, None)
            to_wake = self._settle_queue_locked()
        self._finish(True, to_wake)
        return True

    # the clocks

    def sweep(self) -> bool:
        """Free an agent's lease gone idle past the TTL, or revoke a grant its agent never claimed."""
        to_wake: list[QueuedAgent] = []
        with self._lock:
            if self.controller != Controller.AGENT:
                return False
            now = self.clock()
            # A touch zeroes the grant clock, so a pending grant is one nothing has claimed.
            is_unclaimed = bool(self._granted_at) and now - self._granted_at > CLAIM_WINDOW_SECONDS
            is_idle = now - self._lease_touched_at > LEASE_IDLE_TTL_SECONDS
            if not (is_unclaimed or is_idle):
                return False
            self._write_locked(Controller.NOBODY, None, None, False, None, None)
            to_wake = self._settle_queue_locked()
        self._finish(True, to_wake)
        return True

    def abandon(self, reason: str) -> None:
        """The stack ended: free every queued agent so none waits on a Screen that will not come back by itself."""
        to_message: list[QueuedAgent] = []
        with self._lock:
            waiters, self._wait_queue = self._wait_queue, []
            for waiter in waiters:
                waiter.is_granted = False
                waiter.event.set()
            to_message, self._resume_queue = list(self._resume_queue), []
            self._write_locked(Controller.NOBODY, None, None, False, None, None)
        self._finish(True, [])
        for queued in to_message:
            self.messenger.message_agent(
                str(queued.agent_id),
                queued.agent_name,
                f"The Screen stopped ({reason}), so nothing runs on it until it is started again: "
                "`uv run --no-sync screen control acquire` starts it when you need it.",
            )
