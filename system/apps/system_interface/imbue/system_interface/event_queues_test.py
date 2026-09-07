"""Tests for the agent event queues."""


from imbue.system_interface.event_queues import AgentEventQueues


def test_broadcast_delivers_to_registered_queue() -> None:
    queues = AgentEventQueues()
    q = queues.register("agent-1")
    queues.broadcast("agent-1", {"type": "test", "data": "hello"})
    event = q.get_nowait()
    assert event == {"type": "test", "data": "hello"}


def test_broadcast_batch_preserves_order() -> None:
    queues = AgentEventQueues()
    q = queues.register("agent-1")
    queues.broadcast_batch("agent-1", [{"type": "a"}, {"type": "b"}])
    assert q.get_nowait() == {"type": "a"}
    assert q.get_nowait() == {"type": "b"}


def test_broadcast_does_not_deliver_to_other_agents() -> None:
    queues = AgentEventQueues()
    q1 = queues.register("agent-1")
    q2 = queues.register("agent-2")
    queues.broadcast("agent-1", {"type": "test"})
    assert q1.get_nowait() == {"type": "test"}
    assert q2.empty()


def test_unregister_removes_queue() -> None:
    queues = AgentEventQueues()
    q = queues.register("agent-1")
    queues.unregister("agent-1", q)
    queues.broadcast("agent-1", {"type": "test"})
    assert q.empty()


def test_register_delivers_no_backlog() -> None:
    """Delivery is live-only: events broadcast before a consumer registered are not
    replayed (the REST /events endpoint is the recovery path for history)."""
    queues = AgentEventQueues()
    queues.broadcast("agent-1", {"type": "event-1"})
    q = queues.register("agent-1")
    assert q.empty()


def test_overflowing_consumer_is_evicted_with_a_sentinel() -> None:
    """A consumer that stops draining is evicted on the FIRST overflow: its queue is
    drained and the None sentinel pushed (closing the stream, so the client resyncs via
    reconnect-with-snapshot), and a healthy consumer of the same agent is unaffected."""
    queues = AgentEventQueues()
    stuck = queues.register("agent-1")
    healthy = queues.register("agent-1")

    # Fill the stuck consumer's bounded queue to the brim, draining the healthy one.
    filler = {"type": "filler"}
    is_stuck_full = False
    while not is_stuck_full:
        queues.broadcast("agent-1", filler)
        healthy.get_nowait()
        is_stuck_full = stuck.full()

    # The overflowing broadcast evicts the stuck consumer and still reaches the healthy one.
    queues.broadcast("agent-1", {"type": "overflow"})
    assert healthy.get_nowait() == {"type": "overflow"}
    assert stuck.get_nowait() is None
    # An evicted consumer receives nothing further.
    queues.broadcast("agent-1", {"type": "after"})
    assert stuck.empty()
    assert healthy.get_nowait() == {"type": "after"}


def test_shutdown_sends_none_to_all() -> None:
    queues = AgentEventQueues()
    q1 = queues.register("agent-1")
    q2 = queues.register("agent-2")
    queues.shutdown()
    assert q1.get_nowait() is None
    assert q2.get_nowait() is None
    assert queues.is_shutdown


def test_register_after_shutdown_returns_closed_queue() -> None:
    queues = AgentEventQueues()
    queues.shutdown()
    q = queues.register("agent-1")
    assert q.get_nowait() is None
