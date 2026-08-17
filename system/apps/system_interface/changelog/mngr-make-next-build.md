Fix `agent_manager_test.py` for the refreshed `system/vendor/mngr`: mngr's `make_agent_removed_event` now requires a `host_id` argument, so the three test callsites pass `agent.host.id`. This also clears the `test_no_type_errors` ratchet failures the signature change introduced.


