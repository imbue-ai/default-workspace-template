# Artifact: service

The artifact is a web service -- a scaffolded Flask lib under `libs/<package>/`,
registered in `supervisord.conf`, reachable at `/service/<name>/` through the
system_interface proxy. Load this alongside `harden-artifact.md` and your
operation reference.

## Crystallize = pre-existing, confirmed-live

For the crystallize operation, the service **already exists on disk**: the lead
scaffolded it, built the real routes, and the user signed off on how it looks
and works in the foreground. So the crystallize **pre-existing/confirmed-live
shape** applies -- nothing is reconstructed, there is no outline gate, and the
live confirmation stands in for the final gate. Your job is to prove it works
under test, harden it, and pass the review gates, then report `done`. (Update
and heal operate on the same on-disk service when a later change or a bug
arrives.)

## Where the source lives

- The scaffolded lib: `libs/<package>/src/<package>/runner.py` (the Flask app
  and routes), plus its `pyproject.toml`, `README.md`, and
  `test_<package>_ratchets.py`. Your task file names the exact package and
  service name.
- The service entry in `supervisord.conf` and the matching root `pyproject.toml`
  workspace wiring -- you normally do not touch these; the lead's build created
  them.

## How to run and test it (isolated, never the live service)

- Your fresh worktree has no `.venv`, so run `uv sync --all-packages` once
  before any `uv run`.
- If a fix needs a new dependency, add it the normal way (`uv add ...`) and
  commit the manifest changes (`pyproject.toml` / `uv.lock`) on your branch.
- Exercise the app **in-process** -- drive the Flask app with its test client
  (`app.test_client()`), or launch a **throwaway** threaded Werkzeug server
  (`run_simple(..., threaded=True)`) on an alternate port (never `8000`, never
  the service's live port). Never restart or curl the live service.
- For browser-level verification, drive Playwright against that isolated
  instance. The `build-web-service` skill's `references/verify.md` describes the
  curl-then-Playwright recipe; adapt it to your isolated port, not the live
  proxy.

## Testing specifics

Apply the universal testing/hardening and review-gate contract from
`harden-artifact.md`, with these service specifics:

- The real routes are what you test -- assert on markers true if and only if
  each route behaves correctly (status, rendered content, the raw-data/source
  affordance, empty and overflow states). Add a `test_<package>.py`, plus
  Playwright coverage where the value is in the rendered UI, not just the JSON.
- Run every suite that applies: `cd libs/<package> && uv run pytest` (or the
  repo-root invocation the project uses), plus the ratchets in
  `test_<package>_ratchets.py`.

## Final-gate body template (update / heal only)

For an update or heal, the user must approve the change at the `final-artifact`
gate:

```
<Updated | Fixed> service `<name>`:
- Change: <one-sentence (heal: root cause + fix)>
- Routes affected: <list>
- Scenarios / tests run: <list, all pass>

Approve and save? (yes / no with notes)
```

(Crystallize uses no final gate -- the user confirmed the live site already.)

## Don't-touch

- Do not restart the live service, do not run `layout.py open`/`refresh`/`list`
  against the served tree, and do not try to "reveal" your work. Revealing a
  service is trivial and is the lead's job after merge (a tab refresh) -- it is
  not the life-or-death live-UI reveal the system interface needs, so there is
  deliberately no reveal/rollback machinery here.
- Do not touch `apps/system_interface` or `libs/web_server/`.
- Your job ends at a committed, verified branch. The lead merges it and
  refreshes the user's tab.
