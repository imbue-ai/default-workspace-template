# Outcome verification: grading the artifact, not just the conversation

## Purpose and scope

The harbor persona evals (`apps/minds_evals`, design in [concise.md](concise.md)) currently grade only how the workspace agent *talks*: conciseness, non-technical language, proactivity, and a wordiness guard, all computed from the chat transcript.
The thing the agent was asked to build -- the to-do app, the landing page -- is never examined.
An agent that chats beautifully and ships nothing outscores one that ships a working app in terse messages.

This spec enumerates the space of outcome verification (checking the delivered artifact itself), maps each option onto what harbor, rewardkit, and the Minds workspace already provide, and specifies the design: a per-case `expectations` block in the eval config, a trial-time evidence-collection phase in the driver, and a grade-time `outcome` scoring dimension (programmatic checks plus an LLM judge over the evidence).

Audience: whoever implements this in `apps/minds_evals`, and reviewers deciding the open questions at the end.
Related: the `minds-eval` GitHub issues (scoring fidelity in #706, dev-loop items and the harness-axis note in #708).

## The topology constraint everything follows from

One fact shapes the whole design.
The verifier is a separate slim container that runs **after the trial**, sees only the files declared in `task.toml`'s `artifacts` list, and must stay re-runnable via `harbor trial regrade`.
By the time it runs, the nested workspace sandbox -- the only place the built app ever lived and served -- has been destroyed by the driver's teardown.

So the design splits every verification idea into two halves:

- **Evidence collection** runs at trial time, in the driver, after the last turn and before teardown, while the workspace (and the app inside it) is still alive.
  It produces files under `/logs/agent/verification/`, declared as artifacts.
- **Judgment** runs at grade time, in the verifier, over the captured evidence.
  It is pure and replayable: `harbor trial regrade` re-scores old trials under new judge prompts and check logic without re-running anything.

Anything that needs the live app (HTTP probes, UI automation, running tests) is evidence collection.
Anything that interprets results (pass/fail rules, the LLM judge) is judgment.
This preserves the property the harness already has for transcripts: the expensive, unrepeatable part is captured once; the scoring policy can evolve and be re-applied.

**Note:** harbor's `environment_mode = "shared"` (verifier runs inside the trial environment) does not help here: harbor rejects `regrade` on shared-mode tasks, and even in shared mode the verifier would run in the *box*, not in the destroyed workspace.

## The verification ladder

The options, ordered from cheapest/weakest to most expensive/strongest.
Each level states what it proves, what it cannot, where it runs, and what it needs.
The levels compose; the design adopts all of them behind one schema, phased (see Phasing).

### Level 1: static artifact checks (what files exist)

Check that the workspace tree contains what a real deliverable would: an app directory, an `index.html`, a supervisord entry, git commits beyond the template's.

- **Proves:** the agent produced *something* in the right shape. Catches the all-talk-no-code failure outright.
- **Cannot prove:** that any of it runs or works.
- **Where it runs:** grade time.
  The evidence is a file inventory (path, size, mtime for every file under the workspace home tree, snapshot excludes applied, capped at 20k entries) that the driver writes to `verification/file_inventory.jsonl`.
  Programmatic criteria glob against the inventory; it is a programmatic-only input, not a judge file -- at its own 20k-entry cap the JSONL runs 2-3 MB, past rewardkit's 1 MiB judge-file limit, so it is deliberately absent from the judge's file list.
  The existing home-tree snapshot tarball stays a debugging artifact rather than becoming a grading input by default: shipping ~90 MB into the verifier on every grade and regrade to answer "does `apps/*/` exist" is the wrong tool, and rewardkit's judge skips any file over 1 MB anyway.
  Note the glob check is our code (a custom `@criterion` over the inventory), not rewardkit's stock `file_exists` -- the built-ins test real paths on the verifier's filesystem, which never contained the workspace tree.
- **Opt-in for content checks:** the inventory carries only names/sizes/mtimes, so a case that needs *content*-level assertions (`file_contains` on real source, `sqlite_query_equals` on a db file) can opt into the heavier path: declare the final snapshot under a stable name (`final_home.tar.gz`) in `artifacts`, add a `test.sh` pre-step that extracts it into rewardkit's workspace dir, and then the stock built-ins run against the real tree.
  Enforcement lives in the **driver**, not the generator: `snapshot_mode` is a run-time agent kwarg the generator never sees, and snapshots are natively named `post_message_<turn>.tar.gz` -- so for a case declaring content checks the driver writes the stable-named `final_home.tar.gz` copy unconditionally, overriding `snapshot_mode=off`, because a declared artifact the driver might not produce poisons `regrade` (see Driver changes).
  The case pays the ~90 MB per grade; the snapshot excludes (`node_modules`, `.venv`, `dist`, `build`, and the rest of `SNAPSHOT_EXCLUDES`) still apply, and `command_succeeds` remains out of reach regardless -- the tree is there but the toolchain is not.
- **Needs:** the inventory writer in the driver (one `find`-shaped exec via the existing bridge) plus glob-matching criteria in `tests/`; for content-check cases, the tarball artifact and extract pre-step.

### Level 2: the agent's own tests

Run the test suite the agent wrote for its app (if it wrote one) inside the workspace, and record the outcome.

- **Proves:** the code is at least internally consistent by its author's own standard, and the project's toolchain actually runs.
- **Cannot prove:** much, adversarially -- self-written tests can be vacuous, and their absence is common unless the prompt asked for them.
  This is a *signal for the judge*, never a gate: gating on it would punish cases whose prompts never mentioned tests and reward agents that write one trivial assert.
- **Where it runs:** evidence collection.
  The expectations block may declare `test_commands` (explicit commands run in the workspace project dir via the existing `run_in_workspace` bridge, each with a timeout).
  The driver records each command, exit code, and a bounded output tail into the evidence manifest.
  No auto-discovery of test suites in v1: guessing how to invoke arbitrary generated projects is exactly the kind of flaky heuristic the gates already suffer from (the stub-reply gate, #706).
- **Needs:** `test_commands` in the schema, one collection step in the driver.

### Level 3: liveness probes (is the thing actually being served)

Check that the delivered app is running and answering, the way Minds itself defines "running": registered in the workspace's app/port registry and reachable over HTTP.

- **Proves:** there is a live server behind the claim. Catches "wrote the code, never started it" and "started it, then it crashed".
- **Cannot prove:** that the app does what was asked -- a 200 with a stack-trace page passes a naive probe.
- **Where it runs:** evidence collection.
  The workspace already has ground truth: apps built by the mind register their ports in `data/.state/apps.toml` (via `system/scripts/forward_port.py`) and run as supervised services.
  The driver captures, via `run_in_workspace`:
  1. `data/.state/apps.toml` verbatim, and the derived list of **delivered** apps -- the "delivered is narrower than not pre-existing" rules in the deliverable section apply here at capture, not just at scoring: boot-registered internal daemons (owner-exec on every workspace, vm-exec on cloud slices) and isolated-instance throwaway rows must not be probed, or every trial's judge-visible evidence carries a guaranteed-failed probe of something nobody shipped.
  2. `supervisorctl status` output.
  3. For each **delivered** app, and for each explicitly declared `http` expectation: an in-workspace `curl` (status code, response-time, headers, body up to 256 KB) recorded under `verification/http/`.
- **Important framing:** the harness probes the app **as delivered** -- it never starts the app itself.
  Minds' promise to the client is a running app tab, so "the agent built it but never started/registered it" must score as a delivery failure, not get silently repaired by the harness.
  (Booting the deliverable in a *fresh* environment is a different, complementary measurement -- see "Fresh-environment verification" below.)
- **Needs:** the capture steps above; nothing new in the workspace or box images (`curl` and the bridge already exist).

### Level 4: behavioral verification through the UI

Exercise the app the way the client would: add a TODO, reload the page, see it survive.
This is the only level that can verify the actual promises in the prompt.

Two sub-options were considered for who drives the browser:

- **Scripted flows (not the v1 default, schema home reserved):** per-case scripts with fixed selectors.
  For a freely-invented UI there are no stable selectors to script against across trials, so scripts would mostly measure their own brittleness -- hence not the default.
  But for scenarios anchored in an *existing* application (a case that starts from a known app and asks for a change), the UI is well-defined and a deterministic script is the better instrument.
  The schema reserves the field now: a `ui_flows` entry may carry `script` (a per-case file shipped with the task, mutually exclusive with `steps`/`expect`) instead of prose; execution semantics land when the first such case exists.
- **LLM-driven flows (the v1 default):** each case declares `ui_flows` as *natural-language* step sequences with a verifiable end condition ("Open the app. Add a task named 'buy milk'. Reload the page. The task named 'buy milk' is still visible.").
  A host-side verification agent -- an LLM loop living in the driver, sibling to the decider -- executes each flow and emits a verdict plus evidence.

Execution vehicle: a **box-side browser driving the app's forwarded origin** -- see [flow_executor_forwarded_origin.md](flow_executor_forwarded_origin.md) for the executor's own spec.
The verification agent (the host-side LLM loop) is unchanged; what executes its actions is Playwright + Chromium in the box, navigating `https://<label>.agent-<hex>.localhost:8431/` -- the app's own label on the workspace's agent-keyed origin -- served by the `mngr forward` plugin over its per-host SSH tunnel.
Per-step evidence keeps the same shape: a textual DOM digest (Playwright's accessibility snapshot plus URL/title, standing in for the fleet's browser_use digest) recorded verbatim in the flow log, and a screenshot per step.

Two target **surfaces** exist for a flow, both kept open in the schema (`surface` per flow, default `"origin"`):

- `"origin"` (v1): navigate straight to the delivered app's forwarded origin -- the iframe's `src`.
  Exercises the real product serving path (forward proxy, tunnel, label origin, the proxy's family-scoped session cookie and auth) without the Minds chrome; simplest automation, one origin, no frame-piercing.
- `"minds-ui"` (reserved): drive the full Minds client UI at the bare `agent-<hex>.localhost` origin, reaching the app *as an embedded iframe* in the workspace chrome.
  The only surface that can catch works-at-origin-but-broken-when-iframed failures and exercise minds-level login/tab UX; heavier automation (frame-piercing, chrome noise, a failure-attribution layer between chrome and app), deferred until an origin-surface run motivates it.

**History: the v1 executor was the workspace's own browser fleet, and it was replaced deliberately.**
Phase 2 first shipped flows through `agentic-browser-fleet` (in-workspace Chromium, driven over the `run_in_workspace` bridge) and proved the whole flow-verification concept live -- but the shape had two structural defects surfaced in review:
it coupled the eval to the workspace's internal-tool security model (the fleet's SSRF guard blocks every delivered-app origin, and the eval only ran by changing the product -- dwt PR #462's allowlist -- leaving eval capability contingent on that security posture holding), and it reached the app *under* the product rather than through it (raw `localhost:<port>` inside the container, wrong origin, no serving layer, no origin-scoped auth -- so the forwarding path and everything cookie/session-shaped went unverified).
dwt's own `build-app` flow verifies with a private Playwright instance rather than the fleet, which in hindsight was the design signal.
The forwarded-origin executor resolves both defects: its only product dependency is `mngr forward` -- mngr-owned, versioned in this repo -- and it tests the surface the client actually receives.
dwt PR #462 splits accordingly: its guard *hardening* remains worth landing on its own merits; its allowlist half is no longer needed by the eval.
The fleet's `task` mode remains out of bounds regardless (unobservable in-workspace reasoning, and it bypasses the guard entirely).

**Flow economics:** under the fleet executor, every step was a bridged box-to-workspace round trip (~20 s measured; batched to one exec per step, ~30 s/step live).
The box-side executor removes the workspace hop from the action path -- browser actions are box-local against the forward proxy -- and measured **~4 s per step** on its live proof trial against dwt main (both flows passed, no instrument errors), a 5-7x improvement that also retired the eval's dwt-side dependency.

Coupling caveat, restated for the new executor: this still grades the product using product machinery -- now `mngr forward` instead of the fleet.
That is the right dependency (the eval and the plugin version together in this repo), but the manifest must still record executor-level errors (browser launch failed, forward proxy unreachable, tunnel down, TLS refused) distinctly from flow-level failures (browser worked, app did not), so an infrastructure regression is diagnosable and does not silently read as "the agent builds bad apps".

- **Proves:** the delivered app performs the promised behaviors end to end, including persistence across reload.
- **Cannot prove:** qualities not expressible as short flows (visual polish, responsiveness); and verdicts are LLM-judged, so they carry judge noise like every other judged criterion.
- **Where it runs:** evidence collection performs the flow and records what it saw -- the step log, the screenshots, whether the flow *completed* its declared steps, and the agent's own description of the final page.
  Whether the flow's `expect` holds is decided at grade time by the outcome judge, from that evidence, and only there: one question, one ruling, and one that regrade can revisit.
- **Needs:** the verification-agent loop in the driver (decider-shaped: same API-key plumbing, reported as harness spend under `metadata.verifier_agent_usage`), `ui_flows` in the schema, per-step evidence recording.

### Level 5: state-dump and vision judging

Feed the captured per-step DOM digests and screenshots to the grade-time judge and score what the app *is and looks like* against the expectations ("a hero headline, three feature cards, an email signup form").
The DOM digests are the cheap, token-dense first tier (text, no 1 MB image constraint, diffable across steps); screenshots add what text cannot carry (layout, styling, visual brokenness).

- **Proves:** the visible result matches the described deliverable; catches an unstyled or wrong page that answers 200 and even passes mechanical flows.
- **Cannot prove:** behavior; and a static mock screenshots identically to a working app -- which is exactly why this level rides on Level 4's screenshots (taken mid-interaction) rather than a single load-and-shoot.
- **Where it runs:** grade time, natively: rewardkit's judge inlines `.png`/`.jpg`/`.webp`/`.gif` files from its `files` list as base64 image blocks, skipping any file over 1 MiB with a visible `[skipped: file too large]` block (the judge prompt must attribute that to the harness, not the agent).
  The cap is owned by the grade-time pre-step, not the driver: the driver captures every step's screenshot (bounded only by the per-flow step budget) so regrade retains full evidence, and the pre-step selects at most 8 for the judge; flow logs are tail-capped under the same 1 MiB limit at render time.
- **Needs:** Levels 3-4 capturing screenshots, plus a grade-time pre-step in `test.sh` (alongside `render_expectations.py`) that selects the screenshots and **flattens flow evidence to stable top-level paths** for the judge's `files` list.
  This pre-step is load-bearing, not cosmetic: rewardkit expands a listed directory one level deep, files only -- subdirectories like `flows/<name>/` are skipped silently -- and a listed path that does not exist renders as a visible `[not found]` block, so the generation-time-rendered judge.toml can neither glob `step_<k>.png` names nor safely name paths that may be absent.

## Fresh-environment verification: re-running the deliverable after teardown

Levels 3-5 probe the app in the workspace that built it.
A Minds app has a defined portable shape -- an app directory in the workspace repo, a supervisord `[program:*]` entry, port registration via `forward_port.py` -- so the deliverable can also be booted in a **fresh** environment and probed there.
This measures a different, stricter promise than live probing: not "was there a running app when the client left" but "is the deliverable durable" -- it survives a workspace restart, and everything needed to run it was actually committed.
Both promises are real product properties (workspaces restart; the `crystallize-creation` flow explicitly promises a committed, merged creation), and they disagree on exactly the interesting failures: an app started by hand but never wired into supervisord passes live probing and fails a fresh boot.

**Status: deferred at review** -- neither mode is scheduled, on the condition that trials capture enough *now* for fresh-env verification to be applied to them retroactively.
That capture is cheap and lands in Phase 1: at evidence-collection time the driver records the delivered repo's committed state as `verification/deliverable.bundle`, an incremental `git bundle <base>..HEAD` holding only the agent's commits, plus the repo's `HEAD` sha and `git status --porcelain` output.
The zero-commit trial is explicit, because git refuses to create an empty bundle: when `HEAD == base` the driver records `commit_count: 0` with no bundle file, and that is agent-side evidence (the ships-nothing case this eval exists to catch), never a collection `error`.
The base is NOT the dwt SHA: the driver interposes its own eval-case commit (the dwt clone with `system/vendor/mngr` overwritten from the box's mngr tree) before the workspace is created, and the bundle is based on that commit's HEAD so the vendor rewrite never rides along.
For the bundle to be replayable later, that base commit must be **reproducible**: the eval-case commit is made deterministic (fixed author/committer identity and dates -- its tree is already a pure function of the dwt SHA, the mngr SHA, and the vendor exclude list), and the manifest records the base sha and the dwt tip sha actually cloned.
A dirty or mid-crystallize repo is thereby visible in the record, and a later fresh-boot harness can regenerate the base clone from the recorded SHAs, verify it reproduces the recorded base sha, and unbundle the agent's commits onto it -- without the trial having paid for a second workspace.

Two modes, for when the deferral ends:

- **Minds-mode (the adopted shape): boot a second workspace from the delivered repo.**
  After the final turn, the driver harvests the workspace repo into the box (the bridge's rsync path, same transport as snapshots), then creates a second workspace from it through the exact same production create path the trial already uses -- `build_create_payload` takes a box-local repo path today.
  The fresh workspace's own bootstrap starts supervisord, which starts the app and registers its port; the driver then runs the same Level 3-5 capture against it (registry, HTTP, UI flows via the forwarded origin of the fresh workspace), tagging every manifest entry `env: "fresh"` versus the first workspace's `env: "live"`.
  Two properties to be aware of, both inherent rather than incidental:
  1. **Only committed state travels.** The create path's transfer is git-based (a local-path source reaches the Modal host via git, not by copying the working tree), so a fresh boot verifies the *committed* deliverable. That is the point -- but it interacts with timing, below.
  2. **Crystallize is asynchronous.** The app-commit flow runs through a background worker after the client confirms the live app; on a short eval conversation the merge may not have landed when the final turn ends. A fresh boot taken immediately would then fail apps the client happily used. The driver therefore waits for repo quiescence before harvesting (bounded: no in-flight worker agents in the workspace listing and a clean `git status` in the workspace repo, up to a `fresh_env_quiescence_seconds` budget); if quiescence is not reached, the fresh-env entries are recorded with status `error`, reason `not_quiescent` -- the deliverable was never in a bootable state to test, which the judge sees as harness-side incompleteness, not agent failure. Whether `not_quiescent` should instead count against the agent (the client left without a durable deliverable) is an open question below.
  Cost: one more nested workspace boot per trial (minutes, plus another Modal environment leak until that fix lands), and the trial's `[agent].timeout_sec` grows by the fresh-env budget.
  This mode reuses every existing mechanism -- create path, bridge, flow executor, evidence schema -- which is why it is the adopted shape.
- **Scripted-mode (rejected for now): boot the app from the snapshot at grade time in the verifier.**
  Extract the snapshot tarball in the verifier container, start the app's service against a pinned dwt runtime, probe it -- which would restore `regrade`-ability for live checks, the one thing trial-time evidence gives up.
  Rejected because the costs pile up: the snapshot deliberately excludes `node_modules`/`.venv`/`dist`/`build`, so every grade would reinstall dependencies (network in the verifier, minutes, and a whole class of new infra flakes that would masquerade as grading failures); the verifier image would have to become the multi-GB dwt system image instead of a slim rewardkit container; and regrade would stop being cheap and pure, which is the property the evidence/judgment split exists to protect.
  Revisit only if a code-quality (as opposed to delivery) eval is ever wanted.

Fresh-env verification depends on `dwt_branch` being pinned to a SHA at generation time; that pin has since landed, so this spec treats it as given.

## What the stack already provides (and where its edges are)

The accounting the design leans on, so nothing here is rebuilt.

**harbor** (pinned v0.21.0):

- `artifacts` in `task.toml`: environment paths pulled from the trial and re-materialized at the same absolute paths in the verifier container. This is the only channel into the verifier; everything below rides it.
- `environment_mode = "separate"` + `harbor trial regrade`: the replayability contract the evidence/judgment split preserves.
- `solution/solve.sh` (oracle): must fabricate a passing evidence bundle so oracle runs exercise the new dimension end to end.
- `[agent].timeout_sec`: already `timeout_seconds + AGENT_TIMEOUT_GRACE_SECONDS`; the verification phase needs its own explicit slice (see Driver changes).

**rewardkit** (0.1.x, the verifier engine):

- Judge tomls: an LLM judge over listed `files`, with `likert` (1-N) and `binary` criteria, per-criterion or judge-level files (per-criterion files force `mode = "individual"`), a `prompt_template`, and weights.
  Files are inlined as text; images become vision blocks; **any file over 1 MiB is skipped with a visible `[skipped: file too large]` block** -- a hard sizing constraint on every judge input.
  `.html`/`.pdf`/office documents convert via markitdown and `image_similarity` needs Pillow, but both live behind optional extras (`documents`, `image`) that the verifier's bare `uvx --from 'harbor-rewardkit==0.1.*'` pin does NOT install -- and the resulting ImportError is uncaught, aborting the entire grading run with no reward file for any dimension.
  Any judge input or criterion needing an extra must add it to the `test.sh` pin explicitly; until then, judge files stay to plain text, JSON/JSONL, and images.
- Programmatic criteria: `@criterion` functions in `.py` files returning bool/float, plus a stock library of criterion factories: `file_exists`, `file_contains(_regex)`, `file_matches`, `json_path_equals`, `http_status_equals`, `http_response_contains`, `command_succeeds`, `command_output_*`, `image_similarity`, `sqlite_query_equals`, `diff_ratio`, `trajectory_*`, and more.
  **Edge:** these run in the verifier container at grade time, so in this topology `http_*` (live requests) and `command_succeeds` (needs the project's toolchain) cannot reach the app -- the same checks are instead performed at trial time and their *recorded results* asserted on with plain criteria over the manifest.
- Dimensions: each subdirectory of `tests/` is a scoring dimension with its own entry in `reward.json` (`gates/` and `quality/` today, `outcome/` added by this spec); `finalize.py` composes the final reward because rewardkit's own aggregations cannot express gating (see concise.md, Implementation corrections).

**The Minds workspace** (default-workspace-template):

- `data/.state/apps.toml`: the authoritative registry of served apps and their ports/origins (written by `forward_port.py`); the template's own apps (`system_interface`, `terminal`, `browser`, `files`, ...) register through the same path a delivered app does, so nothing about a row says which is which.
  The **pre-existing** set -- what the workspace already served before the agent ran -- is measured from the workspace itself, not from a hand-maintained name list, which must track the template and had already drifted (`files`, missing from the list and in BACKOFF, was counted as the deliverable, so flows drove the forward proxy's own error page while the real app went unopened).
  It is read from a single probe taken **before turn 1**, once the workspace has booted and been signed in -- the same `workspace_state` probe the evidence phase runs later -- which answers two questions at once, unioned, because neither is complete alone.
  First, the **app registry as it actually stood**: a measurement rather than an inference, and the only source that sees a template app registering its port from inside the script its supervisord program runs -- `terminal` does exactly that (`system/apps/terminal/run_ttyd.sh`), as do `owner-exec` and the cloud slice's `vm-exec`, so a config-only derivation would score the workspace's own terminal as the case's deliverable.
  Second, the **workspace's own `system/supervisord.conf`**, which the same probe cats and which at that moment is still the pinned template's file verbatim, parsed with the same `forward_port.py --name` join used for the live workspace's service health. This covers a template app whose service is slow enough that it had not registered its port yet: the file is on disk from the moment the workspace is cloned, whatever its services are doing.
  Not the directory names under `system/apps/`: a registry name is a caller-supplied `--name` flag rather than a directory, and a multi-port app registers extra origin-label rows that correspond to no directory at all.
  The union is correct for a dwt fork or branch that ships extra apps, which an eval config may point `dwt_repo`/`dwt_branch` at, and it costs nothing that internal daemons land in the set -- their rows are excluded as `internal` anyway.
  The registry is the half that must be readable: without it the set is **unknown**, never empty, the delivered set is unresolvable, and every entry that depends on it is recorded `error` with reason `preexisting_unknown` rather than promoting every template app to a deliverable. A config section the probe came back without only means that half contributes nothing.
  The manifest carries the resolved set as `preexisting_registrations` so a reader can see what was subtracted, and `null` when it is unknown -- the manifest itself keeps that apart from a workspace that served nothing, since a case with no expectations records no entry that would carry the `preexisting_unknown` reason.
- supervisord: every app's serving process is a `[program:*]` entry; `supervisorctl status` is the process-level health truth.
- The browser fleet (`agentic-browser-fleet`): real Chromium, direct-control CLI, screenshots -- the UI-automation vehicle, already in every workspace.
- `system_interface` HTTP API on workspace-local port 8000, already bridged by `minds_bridge.workspace_curl_json`.

**The driver** (`apps/minds_evals`):

- `run_in_workspace` / `fetch_from_workspace` / `workspace_curl_json`: the exec-and-HTTP bridge every collection step uses; nothing new in transport.
- The decider pattern: a budgeted host-side LLM helper with its own usage accounting -- the template for the verification agent.
- `snapshot_workspace`: stays as is (debugging artifact).

## Task definition changes: the `expectations` block

Each persona case gains an optional `expectations` object.
It is eval-harness configuration -- consumed by the driver and verifier only -- so it travels the existing path (instruction.md's embedded JSON and `tests/case.json`), **not** in the workspace clone (unlike agent-under-test arm config, which must ride the clone).

```json
{
  "id": "todo-app",
  "persona": "Non-technical founder who wants a working web app fast.",
  "prompts": ["Build me a simple to-do list web app: ...", "Sounds good.", "..."],
  "expectations": {
    "outcome": "A working to-do web app delivered as a workspace app tab. Tasks can be added, marked complete, and deleted. The task list survives a page reload.",
    "deliverable": {"kind": "minds-app"},
    "ui_flows": [
      {
        "name": "add-complete-delete",
        "steps": "Open the app. Add a task named 'buy milk'. Mark it complete. Add a task named 'walk dog'. Delete 'walk dog'.",
        "expect": "'buy milk' is visible and shown as completed; 'walk dog' is gone."
      },
      {
        "name": "persistence",
        "steps": "Open the app. Add a task named 'persist me'. Reload the page.",
        "expect": "'persist me' is still visible after the reload."
      }
    ]
  }
}
```

Field semantics:

- `outcome` (string, required if `expectations` is present): the prose the judge grades against.
  This is the piece the eval config has been missing: the task description *for the eval*, alongside the prompts *for the agent*.
- `deliverable` (object, optional): what the case commissions, as a **kind with implied checks** rather than a hand-authored check list.
  `kind: "minds-app"` implies the standard shape of a Minds app: at least one *delivered* app registered in `apps.toml`, its supervisord service running, an HTTP 200 from each delivered app's root path, and the deliverable committed to the workspace repo (the bundle capture; whether commit *cleanliness* is scored is an open question below).
  "Delivered" is narrower than "not pre-existing", in two ways.
  Rows the registry marks `internal = true` are machinery that forwards a port but has no page of its own to show (`forward_port.py`'s own wording; the owner-exec daemon is one and answers 404 on `/` by design) -- counting one both inflates the delivered count and fails the root-path probe on something nobody shipped, which a live trial demonstrated before the exclusion existed.
  And dwt's isolated-instance flow registers preview/throwaway rows through the same `forward_port.py` path -- detached processes with no `[program:*]` entry and no auto-cleanup -- so an abandoned throwaway would otherwise fail the agent on a row that was never the deliverable.
  Row names are caller-supplied flags, so pattern matching cannot identify them; the exclusion goes by the isolated-instance state records instead (`data/.state/isolated-instances/<name>/instance.json` names exactly the rows that runner registered, and `down` deletes the record along with the rows).
  Nor is the registry name a supervisord program name: multi-port apps register extra origin-label rows (`<name>-admin`, `<name>-metrics`) with no program of their own, so the service-health join goes through the `forward_port.py` invocations in `supervisord.conf` (the join dwt's own `migrate_workspace.py` uses), not through name equality.
  A delivered row that no supervised program registers scores `failed` with reason `no_supervised_program`: an app started by hand would not survive a workspace restart, and supervision is part of the minds-app contract.
  Optional fields on the block refine the implied set rather than replace it: `min_registered_apps` (int), `http` (extra probes -- `target` is `"registered-apps"` or a service name, with `expect_status` and optional `expect_body_regex`), `files` (extra inventory globs, paths relative to the workspace home tree; the workspace repo root is `/home/user/workspace`, live-confirmed).
  `kind` is the extension point for other deliverable shapes later (a document, a dataset, a skill); a case with no `deliverable` commissions no artifact (e.g. `greeting`).
- `ui_flows` (list, optional): flows for the verification agent, each yielding a verdict and evidence; an entry carries either `steps` + `expect` (natural language, the v1 path) or the reserved `script` field (a per-case script file, for cases anchored in an existing well-defined app; execution semantics deferred), plus an optional `surface` (`"origin"`, the default, or the reserved `"minds-ui"` -- see Level 4).
- `test_commands` (list, optional): commands run in the workspace project dir; recorded, judge-visible, never gated.
- `fresh_env` (bool, optional, default false): also boot the deliverable in a fresh workspace and repeat the deliverable and `ui_flows` checks there (see "Fresh-environment verification"; the feature is deferred -- the flag is reserved, and the Phase 1 bundle capture keeps old trials replayable once it lands).
- Reserved fields fail loudly, never silently: until their execution semantics land, the generator rejects `fresh_env: true` and a `ui_flows` entry using `script` with a "known but unimplemented" error -- a case author must never get a green generation and a completed trial believing verification ran that never did.

**One schema, both consumers, expanded once.**
The evidence collector (driver) and the grade-time judge/checks read the same expectations by construction: the identical `CaseConfig` JSON travels in `instruction.md` (parsed by the driver) and `tests/case.json` (read by the verifier).
The implied-check expansion happens exactly once, in `generate.py`: it **expands** `deliverable.kind` plus refinements into the explicit per-class check list (`files`, `app`, `http`, commit capture) and writes the expanded form into both copies, keeping the authored form alongside as `authored_expectations` for readability.
Expanding at generation time is what keeps the verifier free of expansion logic -- it is a stdlib+rewardkit container that cannot import this package -- and guarantees the collector can never probe a different set of checks than the judge scores.

Cases without `expectations` (e.g. `greeting`) are untouched: no collection phase beyond the (cheap, unconditional) registry/service/inventory capture, no `outcome` dimension in scoring.
An `expectations` block must expand to **at least one check class**: rewardkit only emits the pooled programmatic reward when criteria exist, so a prose-only block would silently make the outcome dimension 100% judge -- double the judge weight every other case has, breaking exactly the cross-case comparability the fixed split exists for.
The v1 validator enforces this as "`deliverable` must be present" -- stricter than the rule itself, since a `ui_flows`-only block (the anchored-in-an-existing-app shape) also expands to a scored class and satisfies the rationale; the validator is relaxed to admit it when the first such case lands.
Prose-only expectations are therefore rejected at generation time until a degenerate composition is deliberately specified.
Schema plumbing: `PersonaCase`/`CaseConfig` in `data_types.py` gain the parsed `expectations` model; `generate.py` validates it (unknown keys rejected, flows require `steps` + `expect` or `script`, `deliverable.kind` must be a known kind), and `verification_timeout_seconds` is carried into `CaseConfig` -- the driver reads only the instruction's embedded JSON, so a knob that is not carried into it does not exist for it.

## The evidence bundle

Everything the collection phase produces lives under `/logs/agent/verification/` and is declared in `task.toml`'s `artifacts`:

```
verification/
  manifest.json          # the index: every probe/flow/test with typed status and pointers
  file_inventory.jsonl   # one {path, size_bytes, mtime} per file (snapshot excludes, 20k cap)
  apps.toml              # verbatim registry capture
  services.txt           # supervisorctl status output
  http/<n>_<slug>.json   # per-probe: request, status, headers, timing, body head (256 KB cap)
  flows/<name>/step_<k>.png        # screenshots per flow step
  flows/<name>/log.jsonl           # per step: the verbatim DOM digest from `state`, action taken, agent reasoning
  trace.jsonl            # the collector's complete tooling trace (see below)
```

`trace.jsonl` is the evidence collector's own flight recorder: every bridge command it ran and the raw (bounded) output it got back, including the failed ones, in order.
It exists for two consumers: diagnosing failures *caused by the collector* (a fleet command that errored, a curl that hit the wrong port) without re-running anything, and letting a regrade or a harness iteration double-check the collector's work -- a judge or a human can audit whether a `failed` verdict traces back to the app or to the instrument.

`manifest.json` is the contract between collection and judgment.
Every entry carries `status`: `"passed"` / `"failed"` / `"error"`, where **`failed` means the workspace fell short and `error` means the harness could not find out** (fleet CLI crashed, curl transport failed, timeout in collection itself), plus `env`: `"live"` (the workspace that built the app) or `"fresh"` (the fresh-environment boot, when enabled).
The distinction is the same one finalize.py already draws for judge failures: an agent must never score zero because the measuring instrument broke.
The manifest also records `evidence_complete: bool` (no entry has status `error`) and wall-clock spent per phase.

## Driver changes

A verification phase runs in `run()`'s `finally`, after the conversation (including its final snapshot) and before teardown, while the workspace is alive -- the `finally` placement is what covers the timeout and exception paths for free:

1. Always (cheap, even without `expectations`): capture `apps.toml`, `supervisorctl status`, and the file inventory.
2. If `expectations` is present: capture the deliverable git bundle and repo state, run `test_commands`, HTTP probes, then `ui_flows` via the verification agent, writing the evidence incrementally so a mid-phase crash still leaves partial evidence.
3. If `fresh_env` is set (deferred feature): wait for quiescence, harvest the repo, create the fresh workspace, repeat the `app`/`http`/`ui_flows` capture there with `env: "fresh"`, and destroy it (it joins the same teardown sweep -- it lives under the same trial `USER_ID`).

Budget: the phase gets its own wall-clock slice, `verification_timeout_seconds` (config-level, default 1800 -- sized from the measured ~20 s bridged round trip, and a deadline rather than a reservation, so flow-less cases finish in seconds regardless; the fresh-env stage adds its own `fresh_env_timeout_seconds` on top when enabled), *added* to the task's `[agent].timeout_sec` by `generate.py` (now case timeout + verification budget + grace) so verification never competes with the conversation for time and teardown keeps its grace.
Timeouts inside collection carry the failed-vs-error distinction: an app that answers slowly or hangs is bounded *per probe* (curl `--max-time`, per-step flow deadlines) and scores `failed` -- a hanging app is the workspace falling short, exactly what the eval exists to catch -- while `error` with reason `timeout` is reserved for the harness exhausting its own collection budget.

**The declared artifact directory must always exist.**
harbor records a missing declared artifact path as `failed`, and `harbor trial regrade` refuses any trial with a failed artifact entry (an *empty* directory is tolerated) -- so a trial that exits before collection would otherwise become permanently non-regradable.
The driver therefore creates `/logs/agent/verification/` in the box unconditionally at setup, and never declares an artifact path it might not produce (this is also why the content-check opt-in's `final_home.tar.gz` is driver-guaranteed).

Timed-out conversations: step 1 still runs best-effort from every timeout path where a workspace exists (an auth-failure trial still has a live workspace to inspect; only paths where create itself never produced one -- which surface as exceptions -- have nothing); step 2 is skipped -- the trial's gates already zero the reward, so spending minutes driving the UI of an unfinished build buys nothing.

Verification-agent spend is harness spend: reported as `metadata.verifier_agent_usage` next to `decider_usage`, never folded into the workspace agent's cost fields.

## Scoring integration

A third rewardkit dimension, `tests/outcome/`, present only in tasks whose case declares `expectations` (the generator omits the directory otherwise, so rewardkit never emits a partial score for it):

- `checks.py` (programmatic, over the manifest): one criterion per expanded expectation class -- `files_expectations_met`, `app_registered`, `http_expectations_met`, `ui_flows_completed` (the fraction of measurable flows that carried out their declared steps, which is a fact about the run rather than a ruling on the `expect`).
  The file reads `case.json`'s expanded check list at import time and registers only the criteria for classes present in it (whether authored directly or implied by `deliverable.kind`), so an absent class contributes no score in either direction.
  Manifest entries with status `error` are handled per the failure-semantics rules below.
- `judge.toml` (LLM judge): files = the rendered `expectations.md` (the `outcome` prose plus the declared checks), `manifest.json`, `conversation.jsonl`, the flow digest, and each flow's last four screenshots up to 24 in all (any screenshot over rewardkit's 1 MB judge limit is dropped from the judge input but kept as a trial artifact).
  A grade-time pre-step in `test.sh` renders `expectations.md` from `case.json` (the same pattern as `render_judge_transcript.py`), so `harbor trial regrade` picks up rendering changes.
  One criterion, `works_as_expected`, likert 10: "given this evidence, how fully does the delivered artifact meet the stated expectations?".
  The prompt instructs the judge that evidence marked `error` is the harness's failure and not the agent's, that ruling on each flow's `expect` is its own call to make from the step log and the screenshots (the recorded completion says what was done, not whether it worked, and the agent's description of the final page is evidence rather than an answer), and -- because `DECIDE_FROM_PERSONA` turns are free-form and the simulated client may legitimately redirect the build mid-conversation -- that the conversation is provided so a deliverable the client visibly steered away from the scripted expectations is graded against the evolved ask, not the original prose.
  This judge is the "smarter LLM-as-judge": it grades against per-case ground-truth expectations and physical evidence, not vibes about the transcript.
  The judge carries half the dimension via `[judge].weight = 1.0`: rewardkit aggregates a dimension in two levels -- all `.py` criteria pool into ONE programmatic reward of weight 1.0, and each judge toml is a second reward carrying its `[judge].weight` -- so 1.0 yields exactly 50/50 regardless of how many programmatic criteria the case declares.
  (The quality dimension's `weight = 3.0` fits the same model: judge 3/4, wordiness guard 1/4.)

Reward composition (finalize.py):

- Cases without expectations: unchanged -- `reward = gates_all_passed ? quality : 0`.
  Triage note: harbor's job-level aggregate counts a missing dimension as 0, so the aggregate `outcome` mean is structurally depressed on any dataset mixing expectation and non-expectation cases -- read per-trial reward.json, never the aggregate, for that dimension.
- Cases with expectations: `reward = gates_all_passed ? (0.5 * quality + 0.5 * outcome) : 0`, where `outcome` is rewardkit's weighted mean of the dimension's rewards (the pooled programmatic criteria and the judge, half each, per above).
  The 50/50 split between conversation quality and outcome is the headline knob; it says "a great app described badly and a great description of no app are equally imperfect", which matches the product's positioning.
  It is a constant in v1, not per-case configuration -- per-case weights would make rewards incomparable across cases.

Failure semantics, extending finalize.py's existing rule:

- Manifest entries with status `error` are excluded from the programmatic criteria they would have fed (the criterion scores over the remaining entries) -- and because a partially-errored class scored over its survivors is otherwise indistinguishable from a fully-measured one, `finalize.py` stamps the manifest's `evidence_complete` bit and per-class error counts into reward-details, so a transient-bridge-error trial is marked and aggregate comparisons can segment on it (a bridge-reliability regression must not read as an agent improvement).
  An all-errored declared class escalates -- but how it escalates depends on the class's instrument:
  - For the cheap probe classes (`files`, `app`, `http`), an all-errored class means the bridge itself broke, which taints the whole measurement: the trial errors as a grading-infrastructure failure. Dropping the conversation-quality data too is deliberate -- better than publishing a reward whose composition silently differs from every other trial's.
  - For `ui_flows`, the instrument is the browser executor -- a heavyweight dependency that can be legitimately unavailable (browser launch failed, forward proxy unreachable, tunnel down) while every other measurement is fine. An all-errored flow class therefore leaves the flows **unscored** rather than voiding the trial: the class drops out of the pooled programmatic reward, the condition is recorded in reward-details, and the judge is told the flows were unmeasurable.
    The cost is a composition skew -- a flow-less outcome score is not directly comparable to a flowed one -- so such trials are marked and aggregate comparisons must segment on the marker.
  Mechanically the escalation lives in `finalize.py` reading the manifest -- a rewardkit `@criterion` cannot express it, because a raising criterion aborts the whole run with no reward file for any dimension, and a returned 0.0 would grade harness breakage as agent failure.
- The unmeasured-outcome detection signal is **`manifest.json`**, not the directory: the driver creates `verification/` unconditionally at setup (the regrade rule above), so the directory always exists and its absence can never be the signal.
  A case that declares expectations, whose `state.json` says the conversation finished, and whose `manifest.json` is absent or empty = grading-infrastructure failure: the collection phase never ran on a trial that needed it, so no reward file, harbor errors the trial (same path as a judge API failure today).
  On a timed-out or otherwise unfinished trial, partial-or-absent evidence is expected, not an error -- the structural gates already zero that trial's reward, exactly as they do today.
- The case file is the other input `finalize.py` must be able to trust: `case.json` missing, unparseable, not a JSON object, or carrying an `expectations` that is neither an object nor `null` = grading-infrastructure failure.
  The generator writes it into every task's tests directory, so a broken one is a harness invariant violation rather than agent behavior -- and every read of it degrades to "declared no expectations", which would grade a commissioned deliverable as a quality-only case at full weight, indistinguishable from a case that never asked for one.
  Unlike the evidence rules, this one is unconditional: the case file is part of the task, not of the run, so neither a failed gate nor a timeout can make a broken one acceptable.
  A valid case file whose `expectations` is absent or `null` is the legitimate bare case and stays quality-only.
  `checks.py` cannot make this call -- an unreadable case file simply registers no criteria there, because raising would abort every dimension.
- Structural gates are untouched: outcome verification never rescues a trial whose conversation gates failed.

Oracle: `solve.sh` fabricates a green bundle -- a manifest with every declared check `passed`, a plausible `apps.toml`, canned flow logs, no screenshots (the judge prompt states screenshots may be absent) -- so `-a oracle` exercises generation, the new artifacts, both new criteria files, and the composition.
Note the existing caveat (#706) that oracle reward floors are judge-dependent and case-dependent; adding a dimension does not fix that, and the oracle threshold claim stays informal.

## Validity and gaming concerns

- **Self-tests are the weakest evidence.** Kept judge-visible but never gated, per Level 2.
- **A static mock can fool screenshots and even naive flows.** The persistence flow (mutate, reload, re-check) is the standing countermeasure; every app-building case should include one.
- **The decider never verifies.** Today's simulated client says "Sounds good." no matter what; the agent's claims go unchallenged in-conversation.
  This spec deliberately verifies *outside* the conversation instead of making the decider skeptical -- decider changes alter the thing being measured (the conversation) and belong to a separate discussion.
- **Nothing at trial time rules on whether a flow worked.** The trial records completion and evidence; the judge decides satisfaction at grade time and regrade can revisit it, so there is no lenient trial-time verdict to survive into the score.
  The programmatic `ui_flows_completed` criterion is trial-time-frozen, but what it freezes is whether the declared steps were carried out -- a fact about the run that regrade has no reason to revisit.
- **The agent cannot see the probes coming.** Collection starts only after the final turn, so there is no in-conversation tell that this trial is instrumented, and nothing new lands in the workspace clone for the agent to read.
- **Live probing alone cannot see durability.** An app started by hand and never committed or wired into supervisord passes every live check; the fresh-env boot is the countermeasure, and the `env` tag lets the judge see exactly which promise broke.
- **Judge-input sizing is a real limit.** 1 MB per file, hard; body heads, log tails, and screenshot caps are load-bearing, not politeness.

## Phasing

1. **Schema + evidence + static/liveness + judge** (the core): `expectations` parsing and the `deliverable.kind` expansion in the generator, the collection phase minus flows (inventory, registry, services, HTTP, test commands), the deliverable git bundle + repo-state capture (so deferred fresh-env verification stays retroactively possible), the `outcome` dimension with the expanded `files`/`app`/`http` criteria and the `works_as_expected` judge, finalize composition, oracle bundle.
   This alone catches ships-nothing and never-started failures and gives the judge real evidence.
2. **UI flows**: the host-side flow agent, flow evidence, `ui_flows_completed`, screenshots feeding the judge (Level 5 comes along for free). Shipped first over the workspace browser fleet (PR #523, live-proven), then re-executed over the forwarded origin (see the executor spec and phase 2b below).
2b. **Executor swap to the forwarded origin** (PR stacked on #523, branch `maciek/minds-evals-forwarded-origin-flows`): box-side Playwright + `mngr forward`, deleting the fleet command layer and the eval's dependency on dwt #462's allowlist; details in [flow_executor_forwarded_origin.md](flow_executor_forwarded_origin.md).
3. **Expectations for the existing dataset**: write `outcome` prose and flows for `todo-app` and `landing-page`; leave `greeting` bare; measure judge/flow stability across `-k` repeats before trusting the numbers (the same statistical discipline #623 demands of a scheduled run).
4. **Fresh-environment boots** (`fresh_env: true`, Minds-mode) -- deferred at review, not scheduled: the quiescence wait, repo harvest, second-workspace create, and the `env`-tagged second capture pass. The Phase 1 bundle capture keeps this applicable retroactively to trials run in the meantime. Requires the dwt SHA pin to have landed (being handled separately).
5. Later, as needed: `expect_body_regex` refinements, `image_similarity` against reference shots for pixel-stable cases, scripted-mode fresh boot if a code-quality (as opposed to delivery) eval is ever wanted.

## Open questions

1. **The 50/50 quality/outcome split** -- gut-check number; the alternative shapes are outcome-gated (reward = quality only if outcome passes a floor) or outcome-dominant (e.g. 30/70). Needs a decision before Phase 1 lands.
2. **Verification-agent model**: flow driving is mostly mechanical, so a smaller model may do, and a high-`-k` nightly multiplies whatever this costs.
   Proposal: default to the decider model with an `--ak verifier_model=` override, and measure flow stability on a cheaper tier before switching the default.
3. **Should the always-on registry/service capture feed a new structural gate** (e.g. "workspace services healthy") for all cases, or stay observability-only until we have baseline data? Proposal: observability-only first.
4. **Fresh-env `not_quiescent` semantics**: when the deliverable never reaches a committed, quiescent state within the budget, is that harness incompleteness (status `error`, as specified) or an agent failure (the client left without a durable deliverable)? Specified as `error` for v1 because short eval conversations end abruptly in a way real sessions do not; worth revisiting once fresh-env runs produce data on how often crystallize lands within the conversation.
5. **Exact create-transfer semantics for the harvested repo**: the local-path create route moves committed state via git; Phase 4 must verify which transfer mode fires on the box-to-Modal path and that the harvested repo's default branch is what the fresh workspace boots from.
6. **Is committing part of the deliverable contract?** The `minds-app` kind implies bundle *capture*, but nothing yet scores whether the delivered state was actually committed and clean -- and committing may well be within Minds' own expectations of a deliverable (`crystallize-creation` promises it).
   If so, a `repo_clean` check joins the implied `minds-app` set and the bundle alone fully describes the deliverable; if not, uncommitted content lives only in the snapshot tarball and retroactive fresh-boots miss it.
   Parked deliberately; revisit once trials show how often the repo is clean at conversation end.

Resolved since the first draft: `dwt_branch` SHA-pinning is being handled separately before this work starts, so the browser-fleet and fresh-env dependencies on a stable dwt are treated as given rather than open.
