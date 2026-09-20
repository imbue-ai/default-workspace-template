- The flow lab's Chromium is killed after a 3 second SIGTERM grace, and stopping it may take up to 30 seconds before it counts as broken. On a loaded machine `minds-evals flow-lab` (and every Chromium test's teardown) therefore shuts the browser down quietly rather than raising `TimeoutExpired: Command 'flow-lab-chromium' timed out after 5.0 seconds` out of a flow that had otherwise finished.

- The suite runs nightly, several times over, in the `minds evals tests (repeated)` workflow (`just test-minds-evals-repeat` locally), so a flaky test is reported as flaky instead of failing whichever PR draws it.

- The README names `rich` as the one dependency conflict that keeps this project outside the uv workspace.

- `just test-minds-evals` gives its Chromium session a 300 second budget in CI rather than the shared 150 second cap, which that session runs within 25 seconds of; an overrun fails the job with no failing test to point at.

- Every deadline the persona driver's own poll loops enforce, and every wait they take between attempts, goes through an injectable clock, which production fills with the system clock and the unit tests fill with a manual one. (The evidence-collection phase keeps its own monotonic deadline, so a clock step cannot shorten or extend it.) A test that drives a trial to its timeout therefore spends its budget in polls rather than in wall-clock seconds: it reaches the same path on the same poll however loaded the machine is, rather than timing out during workspace bring-up and reporting the wrong failure, and the driver suite runs in about a fifth of the time.

- A UI-flow step whose action navigates waits for the new document to commit -- identified by the frame's loader id rather than by its url -- before it reads the page. A navigation the server is slow to answer therefore cannot have the step capture the page the flow just left and record it as the page it reached, and a navigation to the same url (an app that saves and reloads itself) is recognised as the new document it is.

- The git repositories the test suite builds are created with an environment of their own: no `GIT_*` the caller exported, no user or system config, and a fixed author identity and date. A developer whose global config carries `core.hooksPath` or `commit.gpgsign` can run the suite, the suite can run from inside a git hook without staging into the real checkout, and the same fixture produces the same commit shas on every machine.

- `just test-minds-evals` and `just test-minds-evals-repeat` live in `private.just` with the other recipes for this project, so the public mirror's `justfile` no longer carries recipes for a project it does not ship.
