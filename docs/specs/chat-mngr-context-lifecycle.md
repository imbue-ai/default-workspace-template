# Releasing mngr's provider cache after every chat operation

## The problem

`agent_discovery.py` builds a fresh mngr context for every operation that reaches mngr in-process.
`_get_mngr_context()` (line 69) enters a `ConcurrencyGroup`, loads the config with `strict=False`, and returns the pair `(mngr_ctx, cg)`; each caller exits the group in a `finally` and drops the context.

mngr caches every provider instance it builds in a module-level dict, `imbue.mngr.api.providers._instance_cache`, keyed by `(provider name, id(mngr_ctx))`.
A cached instance holds its `MngrContext`, which holds its `ConcurrencyGroup`, which keeps every process it ran -- with that process's captured stdout and stderr -- in `_processes` after the group has exited.
The only thing that removes a context's entries is `close_provider_instances_for_context(mngr_ctx)`, and nothing in the chat app calls it.
So every operation that builds a provider leaks its context, config, provider, and the output of every subprocess it ran, for the life of the chat server.

Five functions build a context today:

| Caller | Runs on | Leaks per call (measured) |
|---|---|---|
| `read_plugin_config` (:88) | the autocompact enable check | nothing: builds no provider |
| `discover_agents` (:153) | the initial listing at startup, and the loopback `/api/agents` endpoint (the evals bridge) | 2 cache entries, ~156 KB |
| `MngrMessenger.send_to_agent` (:340) | every chat message (claude, pi, opencode; antigravity on queue flush), model-switch lines, the stop button's resend of the queued block | 1 entry, ~165 KB |
| `MngrMessenger.press_key_chord_to_agent` (:371) | the stop button and the shoulder-tap cancel chord | 1 entry, ~167 KB |
| `start_agent` (:389) | opening a chat's terminal, pi's model-switch wake, reviving a not-ready codex agent on send | 1-2 entries, ~39-45 KB (lookup only) |

## Evidence

Measured with tracemalloc on DWT main, through the real mngr code path, with the final keystroke a no-op, on a Mac.

- A known-location send retains ~165 KB per call, flat across 1, 5 and 20 agents on the host.
- Of that, ~40-48 KB is fixed (the context, its config models, the provider).
  The rest is ~2.4x the size of the host's `ps -e -o pid=,ppid=,comm=` output, which mngr's liveness check runs (`base_agent.py:286`) and the group then keeps: ~117 KB on a Mac, an estimated 5-15 KB of `ps` output in a Linux workspace container, so ~50-60 KB per message in production (an estimate, not a measurement).
- `reset_provider_instances()` frees all of it (under 1 KB residual per call), so the cache is the only thing holding a context alive.

The PR body carries the full table.

## Scope

In scope: `agent_discovery.py`, its tests, one chat ratchet, and a changelog entry.

Out of scope: mngr itself -- including the `ConcurrencyGroup` keeping finished processes' output after exit, which is the bulk of each leaked context and is listed under follow-ups -- and how often these operations run.

## Design

### One context manager

`_get_mngr_context` and the `(mngr_ctx, cg)` tuple are replaced by a single context manager in `agent_discovery.py`.

```python
@contextmanager
def mngr_context(
    close_providers: Callable[[MngrContext], None] = close_provider_instances_for_context,
) -> Iterator[MngrContext]:
    cg = ConcurrencyGroup(name="chat-app")
    cg.__enter__()
    try:
        # strict=False: <the existing comment, moved here verbatim>
        mngr_ctx = load_config(get_or_create_plugin_manager(), cg, is_interactive=False, strict=False)
        try:
            yield mngr_ctx
        finally:
            close_providers(mngr_ctx)
    finally:
        cg.__exit__(None, None, None)
```

What the nesting guarantees:

- Every exit closes the context's providers, then exits the group.
  A body that raises still closes; a close that raises still exits the group (the outer `finally`).
- A `load_config` failure exits the group without a close, as today: there is no context to close.
- A close that raises propagates in place of the body's exception, with the body's exception chained as `__context__`.
  `close_provider_instances_for_context` already catches `MngrError` and `OSError` from each `instance.close()` and logs them, so this path is reserved for bugs.

`_get_mngr_context` is folded into the context manager rather than kept as a helper.
It existed only to pair the group with the config load, which is now the context manager's whole job.
Keeping it would leave two functions, and the ratchet below would have to allow `load_config` in both; folding it leaves exactly one permitted call site.

**Why the group is exited by hand rather than with `with cg:`.**
`ConcurrencyGroup.__exit__` wraps whatever exception is in flight in a `ConcurrencyExceptionGroup`, which is an `ExceptionGroup`, not an `MngrError`.
The callers catch `MngrError` (`server.py`'s start and revive endpoints, the pi model-switch wake, `agent_manager`'s discovery), so `with cg:` would turn an `AgentNotFoundError` into an uncaught group.
Today's `cg.__exit__(None, None, None)` in a `finally` exists for that reason, and the context manager keeps it.

**Why the close is a parameter.**
The "close raises" test needs a close that fails, and the package forbids monkeypatching (`test_prevent_monkeypatch_setattr` is at 0).
An injected callable with the real close as its default is the file's own pattern (`MngrMessenger.discover` / `send` / `press`).
Production callers pass nothing.

### The callers

| Caller | Change |
|---|---|
| `read_plugin_config` | `with mngr_context() as mngr_ctx: return mngr_ctx.get_plugin_config(name, config_type)`. Builds no provider, but moves so there is one way to get a context. |
| `discover_agents` | `list_agents(...)` and the `default_host_dir` read are both inside the block; the `AgentInfo` loop stays after it. |
| `MngrMessenger.send_to_agent` | The existing `try` body becomes the `with` body, unchanged. |
| `MngrMessenger.press_key_chord_to_agent` | Same. |
| `start_agent` | Same. |

**`discover_agents` reads `default_host_dir` inside the block.**
Reading it after the block would in fact be safe: `close_provider_instances_for_context` only pops cache entries and calls `instance.close()` on each, never touching the context, and `MngrContext` is a `FrozenModel` whose `config` is immutable.
The read moves anyway, because "nothing from the context is used after the block" is a rule a reviewer can check at a glance and "config is safe, providers are not" is not.

**Everything else that crosses the block boundary is plain data**, so no other post-block code moves:

| Value | Type | Live mngr objects? |
|---|---|---|
| `result.agents` in `discover_agents` | `list[AgentDetails]`: ids, names, paths, timestamps, labels, a `HostDetails` of the same kind | No |
| `MessageResult` in send and press | lists of `str` and `AgentSendFailure` (`str` + enum) | No |
| `known_locations` (from the observe cache) and `discover(...)` matches | `AgentMatch`: ids and names | No |
| `SendFailure` from `_first_failure` | two `str`s | No |
| `find_one_agent` / `resolve_to_started_host_and_running_agent` results in `start_agent` | `DiscoveredHost`/`DiscoveredAgent`, `AgentInterface`/`OnlineHostInterface` | Yes, and discarded inside the block |

### What does not change

- Cost: providers were never reused across operations (a new context is a new cache key), so the only added work is one `instance.close()` per provider per operation, a no-op for the local provider.
- Concurrency: contexts are per call and the cache is keyed by context identity, so concurrent requests never close each other's providers.
  The close runs while the context is still referenced, so its `id` cannot yet have been reused by another context, which is the precondition `close_provider_instances_for_context` documents.
- The `strict=False` behaviour, and its comment, move with the `load_config` call.
- `start_agent`'s docstring still holds: it loads the context exactly as a send does.

## The ratchet

Goal: `load_config` from `imbue.mngr.config.loader`, and the retired name `_get_mngr_context`, are called nowhere in the chat package except inside `mngr_context` in `agent_discovery.py`.

**Placement: a new `system/apps/chat/imbue/chat/test_project_ratchets.py`, not `test_ratchets.py`.**
This diverges from the task as given.
`test_ratchets.py` is the standard set every project defines, and the chat app already keeps its project-specific ratchets in their own file (`test_embed_ratchets.py`, whose docstring says exactly this); the `writing-ratchet-tests` skill says the same.
`system/test_meta_ratchets.py` exempts `chat` from its same-test-set check, so `test_ratchets.py` would pass CI, but it would be the first project-specific test in it.
The file name mirrors `system_interface/test_project_ratchets.py`, whose AST import scan is the model for this one.

Mechanics:

1. Files: every `*.py` under the chat package (`Path(__file__).parent.rglob("*.py")`), test files included.
2. Per file, collect what binds mngr's loader: `from imbue.mngr.config.loader import load_config [as X]` binds the name `X`; `from imbue.mngr.config import loader [as M]` and `import imbue.mngr.config.loader [as M]` bind a module whose `.load_config` attribute counts.
   Relative spellings are not handled: `test_prevent_relative_imports` holds the package at 0.
3. Every `ast.Call` whose callee is one of those names or attributes, or is `_get_mngr_context` in any spelling, is a violation unless the file is `agent_discovery.py` and the call's nearest enclosing `FunctionDef` is `mngr_context`.
4. The chat's own `imbue.chat.config.load_config` (`main.py`, `config_test.py`) never binds a loader name, so it never matches.
5. Violations are reported as `RatchetMatchChunk`s through a `RatchetRuleInfo` whose description says to use `mngr_context()`, with `assert len(chunks) <= snapshot(0)`.

**Test files are in scope.**
`create_defaults_test.py:176` moves onto `with mngr_context() as context:` (same loader, same `strict=False`), so the count is 0 with no allowlist.
Excluding tests would leave a second pattern in the package for the next test to copy, which is what the ratchet exists to prevent.

A parametrized self-test feeds the scanner a temp module with each spelling in step 2, a `_get_mngr_context()` call, and the chat's own `load_config`, and checks what it flags, as `system_interface` does for its import scan.

## Testing

All in `agent_discovery_test.py` unless noted.
Nothing touches real agents or tmux: `MNGR_HOST_DIR` is an empty temp dir and `MNGR_PROJECT_CONFIG_DIR` another (the autouse `_isolate_chat_tests` fixture in `conftest.py`; the file's `isolated_mngr_env` fixture does the same for the messenger tests).
The two entry points that run real discovery need a local-only context, which `prepare_isolated_mngr_host_dir` (`testing.py`) provides by writing a profile with docker and modal disabled into the host dir; wire it as a `conftest.py` fixture.
It has so far only backed a spawned `mngr`, so confirm during implementation that the in-process loader picks the profile up; `discover_agents` can additionally pass `provider_names=("local",)`, as its existing test does, but `start_agent` takes no filter and depends on the profile.

**Cache inspection** goes through a test-only accessor in `testing.py`, `provider_cache_keys() -> frozenset[tuple[ProviderInstanceName, int]]`, reading `imbue.mngr.api.providers._instance_cache`.
One place imports the private name; mngr offers no public view (a follow-up).
It reads without the cache's lock, which is fine in these single-threaded tests.

The invariant every entry-point test asserts: `provider_cache_keys()` after the calls equals the snapshot taken before them.

| Entry point, 3 calls each | How the real context path is exercised | Also asserts |
|---|---|---|
| `send_to_agent` | real messenger with stub `discover`/`send`; the stub `send` calls `get_provider_instance(ProviderInstanceName("local"), ctx)` and records the key it created, so the cache held an entry during the call | every recorded key is absent afterwards (the test is not vacuous) |
| `press_key_chord_to_agent` | same shape, stub `press` | same |
| `discover_agents(provider_names=("local",))` | real `list_agents` on the empty host | returns `[]` |
| `start_agent("no-such-agent")` | real `find_one_agent`, which builds the local provider and then raises `AgentNotFoundError` (`pytest.raises`) | the exception is the bare `MngrError` subclass, not a `ConcurrencyExceptionGroup` |
| `read_plugin_config("autocompact", AutoCompactPluginConfig)` | real config load; builds no provider, so this is the weakest row and the ratchet is what pins its use of the manager | returns the defaults |

**The context manager's own lifecycle:**

- Body raises: inside `with mngr_context() as ctx:`, cache a local provider through `get_provider_instance`, then raise a test-defined exception.
  Assert it propagates unchanged, its key is gone, and `ctx.concurrency_group.state is ConcurrencyGroupState.EXITED` (the group is the one `load_config` wires into the context).
- Close raises: `mngr_context(close_providers=<a close that records the call and raises>)` with an empty body.
  Assert the close's exception propagates and the group is `EXITED`.
  Repeat with a body that also raises: the close's exception carries the body's as `__context__`.

**tracemalloc bound: skipped.**
The per-call size is dominated by the host's `ps` output and allocator noise, so a bound tight enough to catch the leak sits inside normal variance across machines.
The cache-key assertion is exact, and the measurement showed the cache is the sole retainer, so it is the leak test.

**Existing tests:** `test_unknown_config_field_degrades_to_a_warning_not_a_failure`'s docstring names `_get_mngr_context`; update it to `mngr_context`.

## Changelog

`system/apps/chat/changelog/mark-chat-provider-cache-leak.md`, in prose like the neighbouring entries: the user-visible change first (the chat server no longer grows by roughly 50-170 KB for every message, stop press, terminal open, and agent listing, for as long as it runs), then the mechanism (one context manager that closes mngr's cached providers for its context on exit) and the ratchet.

## Follow-ups (mngr, out of scope here)

- `ConcurrencyGroup` keeps every finished process, with its captured output, in `_processes` after `__exit__`; an exited group could drop them.
  That output is most of what each leaked context held.
- A public, test-facing view of the provider cache, so the chat's `testing.py` need not import `_instance_cache`.
